import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import line_common as lc

SELF = 'u' + '1' * 32
ROOM = 'c' + '2' * 32

class MediaEnumTests(unittest.TestCase):
    def test_recovered_media_types_and_filename_correlation(self):
        rows = {str(ct): dict(id=str(ct), **{'from':SELF,'to':ROOM},
            createdTime=100,contentType=ct,contentMeta='{"FILE_NAME":"test.txt"}')
            for ct in (0,1,2,3,5,7,14,18,22)}
        with patch.object(lc, 'scan_memory', return_value=(rows,{},0,0)):
            self.assertEqual({r['contentType'] for r in lc.find_outgoing_media(1,SELF,90,'test.txt')}, {1,2,3,5,14})
            self.assertEqual(lc.find_outgoing_media(1,SELF,90,'other.txt'), [])
            self.assertEqual(lc.find_outgoing_media(1,SELF,101,'test.txt'), [])
        self.assertEqual(lc.fmt_content_type(14),'file')
        self.assertEqual(lc.fmt_content_type(7),'sticker')

from contextlib import contextmanager
from datetime import timedelta
import json
import tempfile
from types import SimpleNamespace
import group_actions as ga
import file_upload as fu
import send_message as sm
import line_cli as cli

class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / '測試 a.mp4'
        self.source.write_bytes(b'original video bytes')
        self.prefix = self.root / 'prefix'
        (self.prefix / 'drive_c').mkdir(parents=True)
        self.args = SimpleNamespace(file_path=str(self.source),file_roots=[str(self.root)],
            max_file_mb=1,file_timeout=1,room=ROOM,chat='group',dry_run=False,self_mid=SELF,display=':test')

    def row(self, **changes):
        row = dict(id='100000000000000001', **{'from':SELF,'to':ROOM},createdTime=100,
                   contentType=14,text=self.source.name,
                   contentMeta=json.dumps({'FILE_SIZE':str(self.source.stat().st_size),'SID':'emf','OID':'server-object'}))
        row.update(changes)
        return row

    def test_mapping_backslashes_and_original_extension(self):
        self.assertEqual(fu.windows_path(str(self.prefix/'drive_c'/'a b.mp4'),str(self.prefix)), 'C:\\a b.mp4')
        self.assertEqual(ga.wine_path('/workspace/測試 a.mp4'), 'Z:\\workspace\\測試 a.mp4')

    def test_staging_preserves_unicode_bytes_and_cleans_success(self):
        with patch.object(fu,'prefix_for_pid',return_value=str(self.prefix)):
            with fu.staged_file(str(self.source),1,1000) as staged:
                dest=Path(staged['path'])
                self.assertEqual(dest.name,self.source.name)
                self.assertEqual(dest.read_bytes(),self.source.read_bytes())
                self.assertTrue(staged['wine'].startswith('C:\\line-uploads\\'))
            self.assertFalse(dest.exists())

    def test_staging_retains_after_possible_commit_exception(self):
        with patch.object(fu,'prefix_for_pid',return_value=str(self.prefix)):
            with self.assertRaises(OSError):
                with fu.staged_file(str(self.source),1,1000) as staged:
                    dest=Path(staged['path']); staged['retain']=True
                    raise OSError('X11 disconnected after click')
            self.assertTrue(dest.exists())

    def test_staging_refuses_oversize_and_symlink(self):
        with patch.object(fu,'prefix_for_pid',return_value=str(self.prefix)):
            with self.assertRaises(OSError):
                with fu.staged_file(str(self.source),1,1): pass
            link=self.root/'link'; link.symlink_to(self.source)
            with self.assertRaises(OSError):
                with fu.staged_file(str(link),1,1000): pass

    def test_path_errors_are_specific(self):
        self.assertIn('不存在',ga.resolve_send_file(str(self.root/'missing'),[str(self.root)],1)[1])
        self.assertIn('允許',ga.resolve_send_file('/etc/hosts',[str(self.root)],1)[1])
        self.assertIn('超過',ga.resolve_send_file(str(self.source),[str(self.root)],.000001)[1])
        self.assertEqual(ga.resolve_send_file(self.source.name,[str(self.root)],1)[0],str(self.source))

    def test_verifier_rejects_wrong_identity_local_pending_and_unrelated_files(self):
        good=self.row()
        def hits(row,before=set()):
            return fu.media_candidates({row['id']:row},SELF,ROOM,90,before,str(self.source),self.source.stat().st_size)
        self.assertEqual(len(hits(good)),1)
        for changes in ({'from':'u'+'9'*32},{'to':'c'+'9'*32},{'createdTime':89},
                        {'id':'l-local-uuid'},{'contentType':7},{'text':'other.mp4'},
                        {'contentMeta':'{"FILE_SIZE":"999"}'},{'contentMeta':'invalid'}):
            self.assertEqual(hits(self.row(**changes)),[],changes)
        self.assertEqual(hits(good,{good['id']}),[])

    def test_ambiguous_new_rows_are_uncertain(self):
        rows={r['id']:r for r in [self.row(),self.row(id='100000000000000002')]}
        with patch.object(lc,'find_line_pid',return_value=1), patch.object(lc,'scan_memory',return_value=(rows,{},0,0)):
            self.assertIsNone(fu.verify(1,SELF,ROOM,90,set(),str(self.source),self.source.stat().st_size,0))

    def test_send_locks_selection_releases_for_verification_and_cleans(self):
        held=[]
        @contextmanager
        def lock(timeout=15,name='ui'):
            held.append(name)
            try: yield
            finally: held.remove(name)
        def choose(*a,**kw):
            self.assertIn('ui',held); kw['on_commit'](); return True
        def verify(*a):
            self.assertNotIn('ui',held); self.assertIn('upload',held); return self.row()
        ctx=dict(display=':test',wid='1',pid=1,target=ROOM)
        with patch.object(fu,'prefix_for_pid',return_value=str(self.prefix)), patch.object(lc,'find_line_pid',return_value=1), \
             patch.object(lc,'scan_memory',return_value=({}, {},0,0)), patch.object(sm,'_ui_lock',side_effect=lock), \
             patch.object(sm,'pick_main_window',return_value=('1',None)), patch.object(sm,'_dismiss_file_dialogs') as dismiss, \
             patch.object(sm,'_prepare_chat',return_value=(ctx,None)), patch.object(sm,'_route_matches',return_value=True), \
             patch.object(sm,'click_relative'), patch.object(sm,'_line_class_windows',return_value=[]), \
             patch.object(sm,'_attachment_modals',return_value=[]), patch.object(sm,'_fill_file_dialog',side_effect=choose), patch.object(fu,'verify',side_effect=verify):
            result=fu.send(self.args)
        self.assertEqual(result.status,'ok'); self.assertEqual(result.message_id,self.row()['id'])
        self.assertEqual(dismiss.call_count,2)
        self.assertFalse(list((self.prefix/'drive_c'/'line-uploads').iterdir()))

    def test_wrapper_preserves_safe_failure_and_uncertainty_codes(self):
        for committed,rc in [(False,4),(True,6)]:
            result=fu.Result(status='fail',committed=committed)
            with patch.object(fu,'send',return_value=result):
                self.assertEqual(sm.run_file(self.args),rc)
                self.assertIs(self.args.file_result,result)

class DialogTests(unittest.TestCase):
    def test_parent_discovery_requests_tree(self):
        with patch.object(sm,'run_cmd',return_value=SimpleNamespace(stdout=' Parent window id: 0x123 (none)')) as run:
            self.assertEqual(sm._parent_wid(':test','3'),'291')
        self.assertIn('-tree',run.call_args.args[0])

    def test_dialog_rechecks_route_before_commit(self):
        from unittest.mock import Mock
        for room,commit_count in [(ROOM,1),('c'+'9'*32,0)]:
            with patch.object(sm,'_dialog_candidates',return_value=[('2',(0,0,660,488))]), \
                 patch.object(sm,'_dialog_click_target',return_value=('2',(1,1),(2,2),(3,3))), \
                 patch.object(sm,'window_geom'), patch.object(sm,'activate'), patch.object(sm,'click_relative'), \
                 patch.object(sm,'key'), patch.object(sm,'set_clipboard'),patch.object(sm,'paste'), \
                 patch.object(sm.time,'sleep'), patch.object(lc,'current_open_chat_mid',return_value=room), \
                 patch.object(sm,'_window_mapped',return_value=False):
                committed=Mock()
                self.assertEqual(sm._fill_file_dialog(':test','1','C:\\safe.txt',True,1,ROOM,on_commit=committed),room==ROOM)
                self.assertEqual(committed.call_count,commit_count)

    def test_cleanup_dismisses_path_error_then_picker(self):
        with patch.object(sm,'_dialog_candidates',return_value=[('2',(0,0,660,488))]), \
             patch.object(sm,'_dialog_click_target',return_value=('2',(1,1),(2,2),(3,3))), \
             patch.object(sm,'activate'), patch.object(sm,'key') as key, \
             patch.object(sm,'_window_mapped',side_effect=[True,False,False,False]):
            sm._dismiss_file_dialogs(':test','1')
        self.assertEqual(key.call_count,2)

class FileBotAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'request.pdf';self.path.write_bytes(b'pdf')
        self.args=cli.build_parser().parse_args(['ai','--group-bot','--cooldown','0','--min-interval','0'])
        self.args.file_roots=[str(self.root)];self.args.self_mid=SELF
        self.args.schedules_file=str(self.root/'schedules.json')
        self.st=SimpleNamespace(last_send_at=0,last_group={},replies=0)
        self.gx=SimpleNamespace(stop=False)
        self.patch_gap=patch.object(cli,'_wait_gap',return_value=True);self.patch_gap.start();self.addCleanup(self.patch_gap.stop)
        self.no_x=patch.object(sm,'run',side_effect=AssertionError('unexpected text UI'));self.no_x.start();self.addCleanup(self.no_x.stop)

    def sender(self,status='ok',committed=True):
        def send(ns):
            self.assertEqual(ns.room,ROOM)
            self.assertEqual(ns.file_path,str(self.path))
            self.assertEqual(ns.file_roots,[str(self.root)])
            ns.file_result=fu.Result(status=status,committed=committed,size=3,message_id='100000000000000001',detail='test result')
            return ns.file_result.rc
        return send

    def execute(self,actions):
        return cli._execute_actions(actions,'command','group',ROOM,self.args,self.gx,self.st)

    def test_bot_upload_confirms_in_same_room(self):
        with patch.object(sm,'run_file',side_effect=self.sender()),patch.object(cli,'_send_verified_text',return_value=(True,0)) as text:
            handled,sent=self.execute([{'type':'send_file','path':str(self.path),'room':'c'+'9'*32}])
        self.assertTrue(handled);self.assertTrue(sent)
        self.assertIn('已傳送 request.pdf (3 B)',text.call_args.args[1]);self.assertEqual(text.call_args.args[2],ROOM)

    def test_uncertain_upload_reports_and_stops_rest_without_retry(self):
        with patch.object(sm,'run_file',side_effect=self.sender('uncertain')) as upload,patch.object(cli,'_send_verified_text',return_value=(False,4)) as text:
            handled,sent=self.execute([{'type':'send_file','path':str(self.path)},{'type':'send_file','path':str(self.path)}])
        self.assertTrue(handled);self.assertTrue(sent);upload.assert_called_once();text.assert_called_once()
        self.assertIn('不會自動重傳',text.call_args.args[1])

    def test_definite_file_error_receives_clear_reply(self):
        with patch.object(sm,'run_file',side_effect=self.sender('fail',False)),patch.object(cli,'_send_verified_text',return_value=(True,0)) as text:
            self.execute([{'type':'send_file','path':str(self.path)}])
        self.assertIn('傳檔失敗',text.call_args.args[1])

    def test_missing_file_replies_without_upload(self):
        with patch.object(sm,'run_file') as upload,patch.object(cli,'_send_verified_text',return_value=(True,0)) as text:
            self.execute([{'type':'send_file','path':str(self.root/'missing.pdf')}])
        upload.assert_not_called();self.assertIn('不存在',text.call_args.args[1])

    def test_dry_run_upload_has_no_ui_or_schedule_write(self):
        self.args.dry_run=True
        with patch.object(sm,'run_file',side_effect=AssertionError('dry-run upload')):
            self.execute([{'type':'send_file','path':str(self.path)},
                {'type':'schedule_file','path':str(self.path),'at':ga.iso8(ga.now_taipei()+timedelta(minutes=1))}])
        self.assertFalse(Path(self.args.schedules_file).exists())

    def test_file_schedule_claimed_before_ui_uncertain_never_replayed(self):
        store=ga.ScheduleStore(self.args.schedules_file)
        item,err=store.add(ROOM,'group',ga.now_taipei()-timedelta(seconds=1),'','command',file_path=str(self.path))
        self.assertIsNone(err)
        def send(ns):
            self.assertEqual(store.load()[0]['status'],'sending')
            return self.sender('uncertain')(ns)
        with patch.object(sm,'run_file',side_effect=send) as upload:
            cli._dispatch_due_schedules(self.args,self.gx,self.st)
            cli._dispatch_due_schedules(self.args,self.gx,self.st)
        upload.assert_called_once();self.assertEqual(store.load()[0]['status'],'unconfirmed')

    def test_schedule_file_action_and_list(self):
        at=ga.iso8(ga.now_taipei()+timedelta(minutes=1))
        actions,_,_=ga.parse_actions(json.dumps({'actions':[{'type':'schedule_file','at':at,'path':str(self.path),'room':'evil'}]}),5)
        with patch.object(cli,'_send_verified_text',return_value=(True,0)):
            self.execute(actions)
        rows=ga.ScheduleStore(self.args.schedules_file).load()
        self.assertEqual(rows[0]['path'],str(self.path));self.assertEqual(rows[0]['room'],ROOM)
        self.assertIn('request.pdf',ga.format_pending(rows))

    def test_cli_mid_and_path_flow_through_verified_upload(self):
        args=cli.build_parser().parse_args(['send-file','--mid',ROOM,str(self.path),'--file-root',str(self.root)])
        args.self_mid=SELF
        with patch.object(sm,'run_file',side_effect=self.sender()) as upload:
            self.assertEqual(args.func(args),0)
        upload.assert_called_once()

    def test_cli_schedule_file_persists_revalidated_path(self):
        args=cli.build_parser().parse_args(['schedule-file','--mid',ROOM,str(self.path),'--at',
            ga.iso8(ga.now_taipei()+timedelta(minutes=1)),'--schedules-file',self.args.schedules_file,'--file-root',str(self.root)])
        self.assertEqual(args.func(args),0)
        row=ga.ScheduleStore(self.args.schedules_file).load()[0]
        self.assertEqual(row['type'],'send_file');self.assertEqual(row['path'],str(self.path))


    def test_natural_language_mention_resolves_and_confirms_same_room(self):
        import time
        message=dict(id='100000000000000003',**{'from':'u'+'3'*32,'to':ROOM},
            text=f'@mybot send {self.path}',createdTime=int(time.time()*1000),contentType=0,
            contentMeta=json.dumps({'MENTION':json.dumps({'MENTIONEES':[{'M':SELF}]})}))
        raw=json.dumps({'actions':[{'type':'send_file','path':str(self.path)}]})
        seen=set()
        with patch.object(cli,'_call',return_value=raw),patch.object(sm,'run_file',side_effect=self.sender()) as upload, \
             patch.object(cli,'_send_verified_text',return_value=(True,0)) as text:
            cli._group_bot_handle({message['id']:message},{ROOM:'group'},seen,self.args,{},self.gx,self.st)
        upload.assert_called_once();self.assertIn(message['id'],seen)
        self.assertEqual(text.call_args.args[2],ROOM);self.assertIn('已傳送',text.call_args.args[1])


class PendingUploadScanTests(unittest.TestCase):
    def test_scans_continue_while_upload_waits(self):
        import threading
        release=threading.Event()
        scanned=[]
        ns=SimpleNamespace(file_path='file.txt')
        def upload(ns):
            self.assertTrue(release.wait(6))
            ns.file_result=fu.Result(status='ok',message_id='123456789')
            return 0
        def scan():
            scanned.append(True)
            release.set()
        with patch.object(sm,'run_file',side_effect=upload):
            self.assertEqual(cli._bot_upload(ns,SimpleNamespace(file_scan=scan),SimpleNamespace(stop=False)),0)
        self.assertTrue(scanned)
        self.assertEqual(ns.file_result.message_id,'123456789')

    def test_incomplete_basename_search_never_assumes_unique(self):
        def walk(roots):
            yield '/workspace/first/file.txt'
            raise ga.FileSearchTimeout('timeout')
        with patch.object(ga,'_walk_files',side_effect=walk),patch.object(ga,'path_allowed',return_value=True):
            path,error=ga.resolve_send_file('file.txt',['/workspace'],200)
        self.assertIsNone(path);self.assertIn('逾時',error)

    def test_unknown_modal_cancel_never_presses_enter(self):
        with patch.object(sm,'_window_mapped',side_effect=[True,False]),patch.object(sm,'activate'),patch.object(sm,'key') as key:
            sm._cancel_attachment_modals(':test',['2'])
        key.assert_called_once_with(':test','Escape')

    def test_original_image_requires_size_object_and_service(self):
        row=dict(id='100000000000000004',**{'from':SELF,'to':ROOM},createdTime=100,contentType=1,text='',
            contentMeta=json.dumps({'FILE_SIZE':'79','OID':'server-object','SID':'emi'}))
        self.assertEqual(len(fu.media_candidates({row['id']:row},SELF,ROOM,90,set(),'image.png',79)),1)
        self.assertEqual(fu.media_candidates({row['id']:row},SELF,ROOM,90,set(),'image.png',80),[])
