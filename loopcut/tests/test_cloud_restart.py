"""Cloud jobs that outlive Blender (0.2.19): quitting leaves them running, each conversation keeps its jobs, and
reopening it picks them up, with the "finished while you were away" card and the running job's "Safe to close" note.

The real runtime, cloud_jobs, conversations and the contract mock run unchanged; Blender's scene work is a stand-in.
A restart is the module state (cloud._jobs, cloud_jobs._registry, the session) dropped and the conversation loaded
back from its folder.
"""

import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
sys.modules.setdefault('bpy', types.ModuleType('bpy'))
from loopcut import (cloud, cloud_account, cloud_http, cloud_jobs, cloud_restore, conversations, credentials, files,
                     mainthread, scene_diff, spending, state, tools)
from mock_service import MockService


class Blocks(list):
    def get(self, name):
        return next((block for block in self if block.name == name), None)


class Block(dict):
    """An ID datablock: a name and custom properties (apply_result stamps loopcut_provenance on new ones)."""
    def __init__(self, name):
        super().__init__()
        self.name = name


FOLLOWERS = ('Loopcut cloud job', 'Loopcut cloud pickup', 'Loopcut render previews')


class CloudJobsBase(unittest.TestCase):
    """The mock behind the real HTTP client, a signed-in runtime, and an agent-style call of a cloud tool."""
    JOB_SECONDS = 30

    def setUp(self):
        self.service = MockService('--live-tools', 'image_generate,render', '--job-seconds', str(self.JOB_SECONDS))
        self.service.__enter__()
        self.addCleanup(self.service.__exit__, None, None, None)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.calls, self.applied = [], []
        self.handlers = types.SimpleNamespace(exit_pre=[], persistent=lambda fn: fn)
        self.timers = types.SimpleNamespace(is_registered=lambda fn: False, register=lambda *a, **k: None,
                                            unregister=lambda fn: None)
        self.scene = types.SimpleNamespace(name='Scene', render=types.SimpleNamespace(
            resolution_x=3840, resolution_y=2160, resolution_percentage=100, engine='CYCLES'), frame_current=1)
        self.data = types.SimpleNamespace(filepath=str(Path(self.directory.name) / 'garden.blend'), images=Blocks(),
                                          objects=Blocks())
        Path(self.data.filepath).write_bytes(b'BLENDER')  # The file the jobs start in exists.
        path = types.SimpleNamespace(abspath=self.abspath, relpath=self.relpath)
        self.fake = types.SimpleNamespace(data=self.data, path=path,
                                          context=types.SimpleNamespace(scene=self.scene),
                                          app=types.SimpleNamespace(version=(5, 2, 0), handlers=self.handlers,
                                                                    timers=self.timers, background=True))
        self.replace(sys.modules, 'bpy', self.fake, item=True)
        os.environ['LOOPCUT_DATA_DIR'] = str(Path(self.directory.name) / 'data')
        self.addCleanup(os.environ.pop, 'LOOPCUT_DATA_DIR', None)
        self.replace(scene_diff, 'snapshot', lambda: {})
        self.replace(files, 'project_roots', lambda: (Path(tempfile.gettempdir()).resolve(),))
        self.replace(cloud, '_jobs', {})
        self.replace(cloud_jobs, '_registry', {})
        self.replace(state, '_session', state.new_session())
        self.replace(cloud_jobs, '_add_images', self.import_output)
        self.original_load_frames = cloud_jobs._load_frames
        self.replace(cloud_jobs, '_load_frames', self.import_output)
        self.replace(credentials, 'vendor_key', lambda name: 'test-vendor-key')
        self.addCleanup(self.stay)
        self.client = cloud_http.Client(base_url=self.service.url, api_key=self.service.key,
                                        transport=self.transport, client_headers=cloud_http.contract_headers())
        self.replace(cloud_jobs, '_client', lambda: self.client)
        self.control(reset=True, flags={'premium_skills': False})
        self.runtime = cloud.Runtime(self.client, Path(self.directory.name) / 'cloud', signed_in=True)
        self.runtime.refresh_async = lambda: None
        self.replace(cloud, '_runtime', self.runtime)
        self.assertTrue(self.runtime.refresh())
        self.addCleanup(self.cleanup_jobs)

    def abspath(self, value):
        return str(Path(self.data.filepath).parent / value[2:]) if value.startswith('//') else value

    def relpath(self, value):
        return '//' + os.path.relpath(value, Path(self.data.filepath).parent)

    def replace(self, owner, name, value, item=False):
        context = patch.dict(owner, {name: value}) if item else patch.object(owner, name, value)
        context.start()
        self.addCleanup(context.stop)

    def transport(self, request, **options):
        if getattr(self, 'ended', False) and threading.current_thread() is not threading.main_thread():
            raise cloud_http.CloudError('Blender has quit', 'connection_failed')  # Its job threads asked too late.
        self.calls.append((request.method, urlsplit(request.full_url).path))
        return cloud_http.urllib_transport(request, **options)

    def control(self, **options):
        return self.client.request('POST', '/mock/control', options)

    def mock_job(self, job_id):
        return self.client.request('GET', '/v1/jobs/' + job_id)

    def stay(self):
        if hasattr(cloud_jobs, '_leaving'):  # Blender is not quitting after this test.
            cloud_jobs._leaving.clear()

    def cleanup_jobs(self):
        self.stay()
        cloud_jobs.cancel_all()
        deadline = time.monotonic() + 5
        while cloud_jobs.ui_rows() and time.monotonic() < deadline:
            self.pump()
            time.sleep(.01)
        self.join_followers()
        for row in cloud_jobs._registry.values():
            for output in row['job']['outputs']:
                if 'path' in output:
                    shutil.rmtree(Path(output['path']).parent, ignore_errors=True)
        self.pump()

    def join_followers(self):
        """Every job, pickup and preview thread ends before this test's folders go (they write into them)."""
        deadline = time.monotonic() + 8
        for thread in [t for t in threading.enumerate() if t.name in FOLLOWERS]:
            while thread.is_alive() and time.monotonic() < deadline:
                self.pump()
                thread.join(timeout=.05)

    def pump(self):
        while True:
            try:
                fn, future = mainthread._jobs.get_nowait()
            except queue.Empty:
                return
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn())
                except BaseException as ex:
                    future.set_exception(ex)

    def execute(self, name='image_generate', arguments=None, consent=lambda details: True):
        full_name = cloud.PREFIX + name
        prepared = tools.execute(full_name, json.dumps({'prompt': 'tiny prop'} if arguments is None else arguments))
        if prepared.then is None:
            return prepared
        results = []

        def work():
            try:
                with cloud_account.approval_context(consent):
                    results.append(prepared.then(lambda: False, lambda fn: mainthread.run_on_main(fn).result(timeout=5)))
            except BaseException as ex:
                results.append(ex)
        worker = threading.Thread(target=work)
        worker.start()
        deadline = time.monotonic() + 8
        while worker.is_alive() and time.monotonic() < deadline:
            self.pump()
            time.sleep(.002)
        worker.join(timeout=.1)
        self.assertFalse(worker.is_alive(), 'agent continuation did not finish')
        self.pump()
        if isinstance(results[0], BaseException):
            raise results[0]
        return results[0]

    def import_output(self, job):
        """An apply handler's stand-in: one new datablock, which apply_result stamps with the job's provenance."""
        self.applied.append(job['id'])
        self.data.images.append(Block(f"{job['id']} {len(self.applied)}"))
        return [output['name'] for output in job['outputs']]

    def unsaved_quit(self):
        """What the file loses when Blender quits without saving it (or Undo, or a checkpoint restore)."""
        self.data.images.clear()

    def long_image(self):
        descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'image_generate')
        descriptor['latency'] = 'long'

    def start_long(self):
        """A long managed job, followed on its own thread as a turn leaves it. Returns (job id, that thread)."""
        self.long_image()
        before = set(threading.enumerate())
        started = self.execute()
        self.assertTrue(started.ok, started.text)
        followers = [t for t in set(threading.enumerate()) - before if t.name == 'Loopcut cloud job']
        self.assertEqual(len(followers), 1)
        return json.loads(started.text)['job_id'], followers[0]


class QuitTest(CloudJobsBase):
    def account(self):
        import loopcut
        return patch.object(loopcut, 'account', types.SimpleNamespace(on_signed_in=[]), create=True)

    """Item 0: Blender's exit runs exit_pre, then disables every add-on (bpy.utils._on_exit), which reaches
    cloud.unregister. Quitting must leave a detached job running in the cloud; disabling the add-on still cancels."""

    def test_quitting_blender_stops_following_and_never_cancels_the_job_in_the_cloud(self):
        handle, follower = self.start_long()
        with patch.object(cloud, 'refresh_async'), self.account():
            cloud.register()
            self.assertEqual(len(self.handlers.exit_pre), 1)
            for handler in list(self.handlers.exit_pre):  # WM_exit_ex: exit_pre first ...
                handler(True)
            cloud.unregister()  # ... then addon_utils.disable_all() reaches our unregister.
        follower.join(timeout=5)
        self.assertFalse(follower.is_alive(), 'the job is no longer followed here')
        self.assertNotIn(('POST', f'/v1/jobs/{handle}/cancel'), self.calls)
        self.assertIn(self.mock_job(handle)['status'], ('queued', 'running'))
        self.assertEqual(self.handlers.exit_pre, [], 'unregister removes its handler')
        cloud_jobs._registry.clear()  # This Blender has quit: nothing follows its row any more.

    def test_disabling_the_add_on_still_cancels_its_jobs(self):
        handle, follower = self.start_long()
        with patch.object(cloud, 'refresh_async'), self.account():
            cloud.register()
            cloud.unregister()  # Preferences > Add-ons: no exit_pre.
        follower.join(timeout=5)
        self.assertIn(('POST', f'/v1/jobs/{handle}/cancel'), self.calls)
        self.assertEqual(self.mock_job(handle)['status'], 'canceled')


    def test_quitting_cancels_a_job_no_chat_can_pick_up_as_before(self):
        """A rig, a minutes-long call, or one never kept with its chat: nothing would apply it after a restart, so
        quitting cancels it and its credits come back (0.2.18's behaviour); only pickable jobs are left running."""
        handle, follower = self.start_long()
        cloud_jobs._registry[handle].pop('resumable')
        with patch.object(cloud, 'refresh_async'), self.account():
            cloud.register()
            for handler in list(self.handlers.exit_pre):
                handler(True)
            cloud.unregister()
        follower.join(timeout=5)
        self.assertIn(('POST', f'/v1/jobs/{handle}/cancel'), self.calls)
        self.assertEqual(self.mock_job(handle)['status'], 'canceled')

    def test_a_quit_sends_that_cancel_itself_before_blender_ends(self):
        """The review's item 10: Blender's exit ends the job threads before they would ask (0.2.18's quit left its job
        running, and charged), and between exit_pre and the add-ons' disable Blender saves quit.blend while the
        job's thread hears its progress. That thread keeps following the job, and the quit cancels it itself."""
        handle, follower = self.start_long()
        cloud_jobs._registry[handle].pop('resumable')
        self.addCleanup(setattr, self, 'ended', False)
        with patch.object(cloud, 'refresh_async'), self.account():
            cloud.register()
            for handler in list(self.handlers.exit_pre):
                handler(True)
            time.sleep(1)  # Its progress events arrive meanwhile (the mock's change every 0.3 s).
            self.assertTrue(follower.is_alive(), 'still followed: no chat would pick it up')
            cloud.unregister()
            self.ended = True  # Python ends: what the job's thread asks from now on never leaves Blender.
        self.assertEqual(self.mock_job(handle)['status'], 'canceled')
        follower.join(timeout=5)


class SurvivesQuitTest(unittest.TestCase):
    def test_the_render_offer_may_promise_that_a_job_survives_quitting(self):
        # The cloud render offer's choice card reads this before saying quitting is fine and the frames wait.
        self.assertIs(getattr(cloud_jobs, 'SURVIVES_QUIT', False), True)


class Consent:
    """agent._Consent as a long call's card leaves it: the chat it belongs to and what it holds of that chat's spend."""
    settles = False

    def __init__(self, session, hold):
        self.session, self.hold = session, hold

    def __call__(self, details):
        return True

    def chat(self):
        return self.session, None


class RestartBase(CloudJobsBase):
    """A chat's long job, Blender quitting while it runs, and a later Blender opening that chat."""

    def start_recorded(self, seconds=1.0, hold=(60, True)):
        """A user turn's long job in a saved chat: (job id, the chat)."""
        session = state.session()
        session['items'].append(state.item_user('make me a prop'))
        with spending.lock:
            spending.start(spending.ledger(session), {'credits': hold[0]}, hold[1])
        self.service.transport.state.job_seconds = seconds
        self.long_image()
        started = self.execute(consent=Consent(session, hold))
        self.assertTrue(started.ok, started.text)
        return json.loads(started.text)['job_id'], session

    def quit_blender(self):
        """Blender quits: what follows jobs here stops (the jobs go on in the cloud)."""
        cloud_jobs.leave_all()
        cloud_jobs.cancel_all()
        self.join_followers()

    def quit_and_wait(self, handle, status='succeeded'):
        """Blender quits mid-job; the job goes on in the cloud until it is over."""
        self.quit_blender()
        deadline = time.monotonic() + 10
        while self.mock_job(handle)['status'] != status and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.mock_job(handle)['status'], status)

    def relaunch(self, conversation_id):
        """A new Blender: nothing in memory, the chat loaded from its folder and opened (lifecycle.switch_to). The
        previous Blender quit first."""
        self.quit_blender()
        cloud_jobs._leaving.clear()
        cloud_jobs._registry.clear()
        cloud_jobs._adopting.clear()
        cloud_jobs._adopt_errors.clear()
        cloud._jobs.clear()
        session = conversations.load(conversation_id)
        state.replace(session)
        cloud_restore.pickup(session)
        self.settle_threads()
        return session

    def settle_threads(self):
        deadline = time.monotonic() + 5
        while (any(t.name == 'Loopcut cloud pickup' for t in threading.enumerate())
               or not mainthread._jobs.empty()) and time.monotonic() < deadline:
            self.pump()
            time.sleep(.01)
        self.pump()

    def cards(self, session):
        return [i for i in session['items'] if i.get('kind') == 'cloud_back']

    def press(self, session, card, label):
        n = session['items'].index(card)
        cloud_restore.act(session, n, [a['label'] for a in card['actions']].index(label))

    def wait_card(self, card):
        deadline = time.monotonic() + 10
        while card.get('busy') and time.monotonic() < deadline:
            self.pump()
            time.sleep(.02)
        self.assertFalse(card.get('busy'))

    def count_fetches(self):
        """Requests for a job's state that pickup and following again make (cloud_jobs.fetch), by job id."""
        fetched, original = [], cloud_jobs.fetch

        def fetch(client, job_id):
            fetched.append(job_id)
            return original(client, job_id)
        self.replace(cloud_jobs, 'fetch', fetch)
        return fetched

    def lifecycle(self):
        """loopcut.lifecycle, imported against this test's Blender stand-in (it reads bpy.app.handlers on import)."""
        import importlib
        handlers = types.SimpleNamespace(persistent=lambda fn: fn)
        stubs = patch.dict(sys.modules, {'bpy.app': types.SimpleNamespace(handlers=handlers),
                                         'bpy.app.handlers': handlers,
                                         'loopcut.ui.host': types.SimpleNamespace(tag_redraw_all=lambda: None)})
        stubs.start()
        self.addCleanup(stubs.stop)
        sys.modules.pop('loopcut.lifecycle', None)
        self.addCleanup(sys.modules.pop, 'loopcut.lifecycle', None)
        return importlib.import_module('loopcut.lifecycle')

    def wait_status(self, handle):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.pump()
            result = self.execute('job_status', {'job_id': handle})
            if not result.ok or not result.text.startswith('{'):
                return result
            time.sleep(.02)
        self.fail('job_status never finished')



class RestartTest(RestartBase):
    """Item 1: each managed long job is kept with its conversation and picked up when that conversation opens again."""

    def test_a_long_job_is_saved_with_its_conversation_the_moment_it_starts(self):
        handle, session = self.start_recorded(seconds=30)
        saved = json.loads((conversations.root() / session['id'] / 'conversation.json').read_text())
        row = next(r for r in saved['cloud_jobs'] if r['id'] == handle)
        self.assertEqual((row['tool'], row['apply'], row['blend'], row['scene'], row['conversation']),
                         ('image_generate', 'add_image_reference', self.data.filepath, 'Scene', session['id']))
        self.assertEqual((row['held'], row['auto'], row['status'], row['applied']), (60, True, 'running', False))
        self.assertEqual(row['quoted'], self.mock_job(handle)['credits']['reserved'], 'the approved quote')
        self.assertTrue(row['fingerprint'] and row['key'], 'the retry key survives for cloud_account.complete')
        self.assertNotIn('descriptor', row)

    def test_after_a_restart_job_status_fetches_the_job_and_applies_it_once(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        self.assertEqual(self.applied, [])
        reopened = self.relaunch(session['id'])
        result = self.wait_status(handle)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.applied, [handle])
        self.assertEqual(self.execute('job_status', {'job_id': handle}).text, result.text)
        self.assertTrue(next(r for r in reopened['cloud_jobs'] if r['id'] == handle)['applied'])
        cloud_restore.pickup(reopened)  # Opened again in the same Blender (History): nothing twice.
        self.settle_threads()
        again = self.relaunch(session['id'])  # And in a later Blender, the file saved with them.
        self.assertIn('in this file already', self.execute('job_status', {'job_id': handle}).text)
        self.assertEqual(self.applied, [handle])
        self.assertTrue(next(r for r in again['cloud_jobs'] if r['id'] == handle)['applied'])

    def test_another_blender_applying_it_right_now_holds_this_one_back_until_it_is_done(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        self.relaunch(session['id'])
        lock = conversations.root() / session['id'] / 'cloud_jobs' / (handle + '.applying')
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(handle)  # The same chat open in a second Blender, applying it this moment.
        result = self.wait_status(handle)
        self.assertFalse(result.ok)
        self.assertIn('Another Blender is applying', result.text)
        self.assertEqual(self.applied, [])
        lock.unlink()
        self.assertTrue(self.wait_status(handle).ok, 'not kept: it applies once the other is done')
        self.assertEqual(self.applied, [handle])
        self.assertFalse(lock.exists(), 'the lock is given back')

    def test_pickup_settles_the_chats_hold_and_releases_the_retry_key_once(self):
        handle, session = self.start_recorded()
        self.assertEqual(spending.ledger(session)['credits'], 60)
        self.quit_and_wait(handle)
        self.assertEqual(len(cloud_account._pending(self.runtime)), 1)
        reopened = self.relaunch(session['id'])
        charged = self.mock_job(handle)['credits']['charged']
        self.assertEqual(spending.ledger(reopened)['credits'], charged)
        self.assertEqual(cloud_account._pending(self.runtime), {})
        reopened = self.relaunch(session['id'])
        self.assertEqual(spending.ledger(reopened)['credits'], charged, 'settled once')

    def test_the_outputs_belong_to_the_file_and_scene_the_job_started_in(self):
        """The job's file still exists and another is open: it says which, and downloads nothing until then."""
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        self.relaunch(session['id'])
        self.data.filepath = str(Path(self.directory.name) / 'other.blend')
        before = len(self.calls)
        result = self.wait_status(handle)
        self.assertFalse(result.ok)
        self.assertIn('garden.blend, scene Scene', result.text)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.calls[before:], [], 'nothing fetched or downloaded for another file')
        self.data.filepath = str(Path(self.directory.name) / 'garden.blend')
        self.assertTrue(self.wait_status(handle).ok)
        self.assertEqual(self.applied, [handle])

    def test_a_new_tool_version_still_applies_the_job(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'image_generate')
        descriptor['version'] += 1  # A routine catalog bump while it ran; the same apply handler.
        self.relaunch(session['id'])
        self.assertTrue(self.wait_status(handle).ok)
        self.assertEqual(self.applied, [handle])

    def test_a_tool_that_applies_differently_now_says_so_and_never_sticks(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'image_generate')
        descriptor['apply'] = 'import_model'
        self.relaunch(session['id'])
        result = self.wait_status(handle)
        self.assertFalse(result.ok)
        self.assertIn('applies its results differently now', result.text)
        self.assertIn('verified files', result.text)
        self.assertEqual(self.applied, [])
        descriptor['apply'] = 'add_image_reference'  # The catalog fits again: asked again, it applies.
        self.assertTrue(self.wait_status(handle).ok)
        self.assertEqual(self.applied, [handle])

    def test_outputs_past_30_days_say_they_were_deleted(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        self.control(expire_outputs=True)
        self.relaunch(session['id'])
        result = self.wait_status(handle)
        self.assertFalse(result.ok)
        self.assertIn('These files were deleted after 30 days', result.text)
        self.assertEqual(self.applied, [])
        self.assertIn('x-loopcut-contract', cloud_http.contract_headers())
        self.assertEqual(cloud_http.CONTRACT, '0.10')

    def test_a_damaged_record_is_dropped_and_the_conversation_still_opens(self):
        handle, session = self.start_recorded(seconds=30)
        path = conversations.root() / session['id'] / 'conversation.json'
        body = json.loads(path.read_text())
        body['cloud_jobs'].append({**body['cloud_jobs'][0], 'id': '../../etc', 'blend': 7})
        path.write_text(json.dumps(body))
        loaded = conversations.load(session['id'])
        self.assertEqual([r['id'] for r in loaded['cloud_jobs']], [handle])


class ReviewTest(RestartBase):
    """The independent review of R1 (2026-10-26): what stranded paid results, each fixed."""

    # 1. Whether the outputs are in the file is read from the file.
    def test_quitting_without_saving_brings_the_card_and_the_apply_back(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        reopened = self.relaunch(session['id'])
        [card] = self.cards(reopened)
        self.press(reopened, card, 'Load the result')
        self.wait_card(card)
        self.assertEqual(self.applied, [handle])
        self.unsaved_quit()  # Quit without saving: the file never had them.
        again = self.relaunch(session['id'])
        [card] = self.cards(again)
        self.press(again, card, 'Load the result')
        self.wait_card(card)
        self.assertEqual(card['resolved'], 'Loaded into your scene.')
        self.assertEqual(self.applied, [handle, handle])

    def test_undo_after_an_apply_lets_job_status_apply_it_again(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        self.relaunch(session['id'])
        self.assertTrue(self.wait_status(handle).ok)
        self.assertIn('Added image references', self.execute('job_status', {'job_id': handle}).text)
        self.assertEqual(self.applied, [handle], 'asked again while they are in the file: no second apply')
        self.unsaved_quit()  # Ctrl+Z, or a checkpoint restore.
        self.assertTrue(self.wait_status(handle).ok)
        self.assertEqual(self.applied, [handle, handle])

    def test_undo_after_load_brings_the_cards_load_back_and_redo_says_loaded(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        reopened = self.relaunch(session['id'])
        [card] = self.cards(reopened)
        self.press(reopened, card, 'Load the result')
        self.wait_card(card)
        lifecycle = self.lifecycle()
        for name in ('load_post', 'save_pre', 'save_post', 'render_complete', 'undo_post', 'redo_post'):
            setattr(self.handlers, name, [])
        self.assertIn((self.handlers.undo_post, lifecycle._on_undo_redo), lifecycle._handlers())
        self.assertIn((self.handlers.redo_post, lifecycle._on_undo_redo), lifecycle._handlers())
        stamped = list(self.data.images)
        self.unsaved_quit()  # Ctrl+Z: the apply's undo step goes, and the image with it.
        lifecycle._on_undo_redo()
        self.assertEqual((card['resolved'], [a['label'] for a in card['actions']]), ('', ['Load the result']))
        self.data.images.extend(stamped)  # Ctrl+Shift+Z.
        lifecycle._on_undo_redo()
        self.assertEqual(card['resolved'], 'Loaded into your scene.')
        self.unsaved_quit()
        lifecycle._on_undo_redo()
        self.press(reopened, card, 'Load the result')
        self.wait_card(card)
        self.assertEqual(card['resolved'], 'Loaded into your scene.')
        self.assertEqual(self.applied, [handle, handle])

    # 13. A failed apply never keeps the lock.
    def test_an_apply_that_raises_gives_its_lock_back(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        self.relaunch(session['id'])
        broken = patch.object(cloud_jobs, '_add_images', side_effect=AttributeError('a freed datablock'))
        with broken:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                self.pump()
                try:
                    result = cloud._job_status({'job_id': handle})
                except AttributeError:
                    break
                time.sleep(.02)
            else:
                self.fail(f'the apply never ran: {result.text}')
        self.assertFalse((conversations.root() / session['id'] / 'cloud_jobs' / (handle + '.applying')).exists())
        self.assertTrue(self.wait_status(handle).ok)
        self.assertEqual(self.applied, [handle])

    # 3. Which file: an unsaved one asks first; Save As and a moved file are followed or asked about.
    def test_a_job_from_an_unsaved_file_asks_before_it_loads_anywhere(self):
        self.data.filepath = ''
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        self.data.filepath = str(Path(self.directory.name) / 'other.blend')
        reopened = self.relaunch(session['id'])
        [card] = self.cards(reopened)
        self.assertEqual([a['label'] for a in card['actions']], ['Load here anyway'])
        self.assertIn('unsaved file', card['detail'])
        answer = self.wait_status(handle)
        self.assertFalse(answer.ok)
        self.assertIn('unsaved file', answer.text)
        self.assertEqual(self.applied, [])
        self.press(reopened, card, 'Load here anyway')
        self.wait_card(card)
        self.assertEqual(self.applied, [handle])
        record = next(r for r in reopened['cloud_jobs'] if r['id'] == handle)
        self.assertEqual(record['blend'], self.data.filepath, 'it belongs to this file now')

    def test_save_as_takes_the_chats_unapplied_jobs_along(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        garden = self.data.filepath
        renamed = str(Path(self.directory.name) / 'garden-final.blend')
        reopened = self.relaunch(session['id'])
        self.data.filepath = renamed
        cloud_restore.file_saved(reopened, garden, renamed)  # lifecycle._on_save_post after Save As.
        self.assertEqual(next(r for r in reopened['cloud_jobs'] if r['id'] == handle)['blend'], renamed)
        self.assertTrue(self.wait_status(handle).ok)

    def test_save_as_through_blenders_save_handlers_takes_them_along(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        self.relaunch(session['id'])
        lifecycle = self.lifecycle()
        lifecycle._on_save_pre()  # File > Save As garden-final.blend.
        self.data.filepath = str(Path(self.directory.name) / 'garden-final.blend')
        lifecycle._on_save_post()
        body = json.loads((conversations.root() / session['id'] / 'conversation.json').read_text())
        self.assertEqual(next(r for r in body['cloud_jobs'] if r['id'] == handle)['blend'], self.data.filepath)
        self.assertTrue(self.wait_status(handle).ok)

    def test_a_file_moved_away_from_where_the_job_started_asks_first(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        Path(self.data.filepath).rename(Path(self.directory.name) / 'moved.blend')
        self.data.filepath = str(Path(self.directory.name) / 'moved.blend')
        reopened = self.relaunch(session['id'])
        [card] = self.cards(reopened)
        self.assertEqual([a['label'] for a in card['actions']], ['Load here anyway'])

    # 4. A second Blender's card never says it loaded what it didn't.
    def test_a_card_says_another_blender_is_loading_rather_than_loaded(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        reopened = self.relaunch(session['id'])
        [card] = self.cards(reopened)
        lock = conversations.root() / session['id'] / 'cloud_jobs' / (handle + '.applying')
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(handle)
        self.press(reopened, card, 'Load the result')
        self.wait_card(card)
        self.assertEqual(card['resolved'], '')
        self.assertIn('Another Blender is applying', card['error'])
        self.assertEqual(self.applied, [])

    # 5. One failed fetch never keeps a running job from being followed.
    def test_a_failed_fetch_earlier_does_not_stop_the_job_being_followed(self):
        handle, session = self.start_recorded(seconds=30)
        self.quit_and_wait(handle, 'running')
        mock = self.service.transport.state
        mock.job_seconds = time.monotonic() - mock.jobs[handle]['started'] + 1.5
        fetched = self.count_fetches()
        cloud_jobs._leaving.clear()  # A new Blender, whose last fetch of this job failed (offline then).
        cloud._jobs.clear()
        cloud_jobs._registry.clear()
        cloud_jobs._adopt_errors[handle] = cloud_http.CloudError('offline a minute ago', 'connection_failed')
        session = conversations.load(session['id'])
        state.replace(session)
        cloud_restore.pickup(session)
        deadline = time.monotonic() + 10
        while not self.cards(session) and time.monotonic() < deadline:
            self.pump()
            time.sleep(.02)
        self.assertEqual(len(self.cards(session)), 1, 'followed to its end, then its card')
        self.assertEqual(fetched, [handle], "pickup's one request is the one following used")

    # 6. A cancel right after a reopen is never dropped.
    def test_a_cancel_asked_while_the_job_is_being_fetched_reaches_the_service(self):
        handle, session = self.start_recorded(seconds=30)
        self.quit_and_wait(handle, 'running')
        self.relaunch(session['id'])
        cloud_jobs._registry.clear()  # As if its following had not started yet.
        hold, original = threading.Event(), cloud_jobs._remember

        def remember(*args, **kwargs):
            hold.wait(timeout=5)
            return original(*args, **kwargs)
        with patch.object(cloud_jobs, '_remember', remember):
            cloud_restore.follow(cloud._jobs[handle])
            answer = self.execute('job_status', {'job_id': handle, 'cancel': True})
            hold.set()
            self.assertTrue(answer.ok, answer.text)
            deadline = time.monotonic() + 10
            while self.mock_job(handle)['status'] != 'canceled' and time.monotonic() < deadline:
                time.sleep(.05)
        self.assertEqual(self.mock_job(handle)['status'], 'canceled')
        self.assertIn(('POST', f'/v1/jobs/{handle}/cancel'), self.calls)

    # 7. A charge settles in the chat as it is now, never in an older copy written over newer turns.
    def test_a_job_finishing_after_history_reopened_its_chat_settles_in_the_reopened_copy(self):
        handle, first = self.start_recorded()
        copy = conversations.load(first['id'])  # History: the same chat again, as a new copy.
        state.replace(copy)
        copy['items'].append(state.item_user('a newer turn'))
        conversations.save(copy)
        deadline = time.monotonic() + 10
        while not next(r for r in copy['cloud_jobs'] if r['id'] == handle).get('settled') \
                and time.monotonic() < deadline:
            self.pump()
            time.sleep(.02)
        charged = self.mock_job(handle)['credits']['charged']
        self.assertEqual(spending.ledger(copy)['credits'], charged)
        body = json.loads((conversations.root() / first['id'] / 'conversation.json').read_text())
        self.assertIn('a newer turn', [i.get('text') for i in body['items']])
        self.assertEqual(body['spend']['credits'], charged)

    def test_a_job_finishing_while_its_chat_is_closed_settles_in_its_file(self):
        handle, first = self.start_recorded()
        state.replace(state.new_session())  # Another chat on screen.
        deadline = time.monotonic() + 10
        path = conversations.root() / first['id'] / 'conversation.json'
        while time.monotonic() < deadline:
            self.pump()
            body = json.loads(path.read_text())
            if next(r for r in body['cloud_jobs'] if r['id'] == handle).get('settled'):
                break
            time.sleep(.02)
        self.assertEqual(body['spend']['credits'], self.mock_job(handle)['credits']['charged'])
        self.assertEqual(len(body['items']), len(first['items']))

    def test_an_older_copy_never_writes_over_newer_turns(self):
        from loopcut import agent
        old = state.session()
        old['items'].append(state.item_user('first'))
        conversations.save(old)
        live = conversations.load(old['id'])
        state.replace(live)
        live['items'].append(state.item_user('newer'))
        conversations.save(live)
        with spending.lock:
            spending.start(spending.ledger(live), {'credits': 60}, True)
            spending.start(spending.ledger(old), {'credits': 60}, True)
        agent._settle_spend(old, 60, 40, True)  # A detached job's charge, reported to the copy that started it.
        self.assertEqual(spending.ledger(live)['credits'], 40, 'settled in the copy on screen')
        agent._save_spend(old)
        body = json.loads((conversations.root() / old['id'] / 'conversation.json').read_text())
        self.assertIn('newer', [i.get('text') for i in body['items']])

    # 8. What opening a chat costs.
    def test_a_job_known_to_be_over_is_not_asked_about_again(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        fetched = self.count_fetches()
        self.relaunch(session['id'])
        self.assertEqual(fetched, [handle])
        reopened = self.relaunch(session['id'])
        self.assertEqual(fetched, [handle], 'its card comes from the record')
        self.assertEqual(len(self.cards(reopened)), 1)

    def test_the_longest_unasked_jobs_are_asked_about_first(self):
        first, session = self.start_recorded(seconds=30)
        second, _ = self.start_recorded(seconds=30)
        self.quit_and_wait(second, 'running')
        fetched = self.count_fetches()
        with patch.object(cloud_restore, 'CHECKS', 1):
            self.relaunch(session['id'])
            self.assertEqual(fetched, [first])
            self.relaunch(session['id'])
            self.assertEqual(fetched, [first, second], 'the next opening asks about the other one')

    def test_a_running_job_of_another_file_downloads_nothing_until_that_file_opens(self):
        """Following a job downloads its outputs once it is over: never for a file that is not the job's."""
        handle, session = self.start_recorded(seconds=30)
        self.quit_and_wait(handle, 'running')
        mock = self.service.transport.state
        mock.job_seconds = time.monotonic() - mock.jobs[handle]['started'] + 1
        garden = self.data.filepath
        self.data.filepath = str(Path(self.directory.name) / 'other.blend')  # Its chat opened from History there.
        reopened = self.relaunch(session['id'])
        conversations.add_project(reopened, garden)  # The chat garden.blend's opening resumes (resume_or_new).
        self.assertEqual(cloud_jobs.ui_rows(), [], 'not followed for another file')
        deadline = time.monotonic() + 10
        while self.mock_job(handle)['status'] != 'succeeded' and time.monotonic() < deadline:
            time.sleep(.05)
        self.settle_threads()
        self.assertEqual([path for _, path in self.calls if path.startswith('/mock-files/')], [], 'nothing downloaded')
        self.data.filepath = garden  # File > Open garden.blend: lifecycle's load_post, this chat still on screen.
        self.lifecycle().resume_for_open_file()
        self.settle_threads()
        [card] = self.cards(reopened)
        self.press(reopened, card, 'Load the result')
        self.wait_card(card)
        self.assertEqual(self.applied, [handle])

    # 12. A job started here that finished while its chat was closed gets its card when it opens again.
    def test_a_job_started_here_that_finished_while_its_chat_was_closed_gets_its_card(self):
        handle, session = self.start_recorded()
        state.replace(state.new_session())  # Another chat while it finishes, in this same Blender.
        deadline = time.monotonic() + 10
        while cloud_jobs.over(handle) is None and time.monotonic() < deadline:
            self.pump()
            time.sleep(.02)
        back = conversations.load(session['id'])
        state.replace(back)
        cloud_restore.pickup(back)
        self.settle_threads()
        [card] = self.cards(back)
        self.assertEqual(card['title'], 'Image generation finished while you were away')
        self.press(back, card, 'Load the result')
        self.wait_card(card)
        self.assertEqual(self.applied, [handle])


class AwayCardTest(RestartBase):
    """Item 2: a job that finished while its chat was closed gets a card there, with Load and Make the MP4."""

    def cards(self, session):
        return [i for i in session['items'] if i.get('kind') == 'cloud_back']

    def press(self, session, card, label):
        n = session['items'].index(card)
        cloud_restore.act(session, n, [a['label'] for a in card['actions']].index(label))

    def wait_card(self, card):
        deadline = time.monotonic() + 10
        while card.get('busy') and time.monotonic() < deadline:
            self.pump()
            time.sleep(.02)
        self.assertFalse(card.get('busy'))

    # The render copy from the owner's mock-up, from a job and its record alone.
    def render(self, job):
        """A render's record once the service said how it ended (cloud_restore.learn), as the card reads it."""
        saved = {'id': 'job_r', 'tool': 'render', 'title': 'Render scene', 'frames': 1800, 'resolution': [3840, 2160],
                 'blend': self.data.filepath, 'scene': 'Scene'}
        cloud_restore.learn(saved, job)
        return saved

    # The render copy from the owner's mock-up, from a job and its record alone.
    def test_a_finished_render_says_frames_size_time_and_charge_with_make_the_mp4_first(self):
        frames = [{'type': 'render_frames', 'name': f'f{i}', 'mime': 'image/png', 'meta': {'frame': i}} for i in range(1800)]
        preview = {'type': 'image', 'name': 'preview_0001.jpg', 'mime': 'image/jpeg', 'meta': {'frame': 1, 'preview': True}}
        job = {'status': 'succeeded', 'outputs': frames + [preview], 'credits': {'reserved': 700, 'charged': 640, 'refunded': 60},
               'created_at': '2026-10-25T07:20:00Z', 'finished_at': '2026-10-25T09:12:30Z'}
        card = cloud_restore.card(self.render(job), away=True)
        self.assertEqual(card['title'], 'Your cloud render finished while you were away')
        self.assertEqual(card['detail'], '1,800 frames · 4K · took 1 h 52 min · 640 credits charged')
        self.assertEqual([a['label'] for a in card['actions']], ['Make the MP4', 'Load the frames'])
        failed = cloud_restore.card(self.render({**job, 'status': 'failed', 'error': {'message': 'The GPU worker stopped.'},
                                                 'credits': {'reserved': 700, 'charged': 0, 'refunded': 700}}), away=True)
        self.assertEqual((failed['title'], failed['detail'], failed['actions']),
                         ('Your cloud render failed while you were away', 'The GPU worker stopped. · 700 credits were returned', []))
        expired = cloud_restore.card(self.render({**job, 'outputs': [{**frames[0], 'expired': True}]}), away=True)
        self.assertIn('These frames were deleted after 30 days.', expired['detail'])
        self.assertEqual(expired['actions'], [])

    def test_a_partial_render_and_a_canceled_ones_charge_are_said_as_they_are(self):
        frames = [{'type': 'render_frames', 'name': f'f{i}', 'mime': 'image/png', 'meta': {'frame': i}} for i in range(1200)]
        partial = cloud_restore.card(self.render({
            'status': 'succeeded', 'outputs': frames, 'credits': {'charged': 430, 'refunded': 210},
            'partial': {'requested': 1800, 'delivered': 1200, 'cause': 'input', 'message': 'Time limit'}}), away=True)
        self.assertEqual(partial['detail'].split('\n')[1], 'Stopped early: 1,200 of 1,800 frames.')
        self.assertIn('1,200 frames', partial['detail'])
        charged = cloud_restore.card(self.render({'status': 'canceled', 'outputs': [],
                                                  'credits': {'charged': 40, 'refunded': 660}}), away=False)
        self.assertEqual((charged['title'], charged['detail']),
                         ('Your cloud render was canceled', '40 credits charged · 660 credits were returned'))
        free = cloud_restore.card(self.render({'status': 'canceled', 'outputs': [],
                                               'credits': {'charged': 0, 'refunded': 700}}), away=False)
        self.assertEqual(free['detail'], 'Nothing was charged. · 700 credits were returned')

    def test_reopening_shows_the_card_and_load_applies_the_result_once(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        reopened = self.relaunch(session['id'])
        [card] = self.cards(reopened)
        self.assertEqual(card['title'], 'Image generation finished while you were away')
        self.assertEqual([a['label'] for a in card['actions']], ['Load the result'])
        self.assertNotIn('cloud_back', json.dumps(json.loads(
            (conversations.root() / session['id'] / 'conversation.json').read_text())['items']), 'never saved')
        self.press(reopened, card, 'Load the result')
        self.wait_card(card)
        self.assertEqual(card['resolved'], 'Loaded into your scene.')
        self.assertEqual(self.applied, [handle])
        self.assertFalse((conversations.root() / session['id'] / 'cloud_jobs' / (handle + '.applying')).exists())
        cloud_restore.act(reopened, reopened['items'].index(card), 0)  # A second click does nothing.
        cloud_restore.pickup(reopened)  # Nor does opening it again here.
        self.settle_threads()
        self.assertEqual(len(self.cards(reopened)), 1)
        again = self.relaunch(session['id'])
        self.assertEqual(self.cards(again), [], 'no second card once applied')
        self.assertEqual(self.applied, [handle])

    def test_switching_away_and_back_in_the_same_blender_shows_the_card_again_until_loaded(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        reopened = self.relaunch(session['id'])
        self.assertEqual(len(self.cards(reopened)), 1)
        other = state.new_session()  # History: another chat, then this one again (a fresh copy from its file).
        state.replace(other)
        cloud_restore.pickup(other)
        back = conversations.load(session['id'])
        state.replace(back)
        cloud_restore.pickup(back)
        self.settle_threads()
        [card] = self.cards(back)
        self.press(back, card, 'Load the result')
        self.wait_card(card)
        self.assertEqual(self.applied, [handle])
        self.assertTrue(next(r for r in back['cloud_jobs'] if r['id'] == handle)['applied'])

    def test_the_model_answer_after_load_is_the_same_apply(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        reopened = self.relaunch(session['id'])
        [card] = self.cards(reopened)
        self.press(reopened, card, 'Load the result')
        self.wait_card(card)
        self.assertTrue(self.execute('job_status', {'job_id': handle}).ok)
        self.assertEqual(self.applied, [handle])

    def test_make_the_mp4_sends_a_user_turn_naming_the_job(self):
        handle, session = self.start_recorded()
        self.quit_and_wait(handle)
        reopened = self.relaunch(session['id'])
        [card] = self.cards(reopened)
        card['actions'].insert(0, {'label': 'Make the MP4', 'do': 'mp4'})  # A render's card.
        from loopcut import agent
        with patch.object(agent, 'send', return_value=True) as send:
            self.press(reopened, card, 'Make the MP4')
        send.assert_called_once()
        self.assertIn(handle, send.call_args.args[0])
        self.assertIn('MP4', send.call_args.args[0])
        self.assertEqual(card['note'], 'Asked Loopcut to make the MP4.')
        self.assertEqual([a['label'] for a in card['actions']], ['Load the result'], 'Load stays, if the agent fails')
        self.assertEqual(self.applied, [], 'the agent applies them, through job_status')
        self.press(reopened, card, 'Load the result')
        self.wait_card(card)
        self.assertEqual(self.applied, [handle])

    def test_a_canceled_job_says_so_once_with_its_refund(self):
        handle, session = self.start_recorded(seconds=30)
        cloud_jobs.leave_all()
        self.client.request('POST', f'/v1/jobs/{handle}/cancel')
        self.quit_and_wait(handle, 'canceled')
        reopened = self.relaunch(session['id'])
        [card] = self.cards(reopened)
        self.assertEqual(card['title'], 'Your cloud job was canceled while you were away')
        self.assertIn('credits were returned', card['detail'])
        self.assertEqual(card['actions'], [])
        self.assertEqual(self.cards(self.relaunch(session['id'])), [], 'said once')

    def test_a_job_still_running_is_followed_and_gets_its_card_when_it_finishes(self):
        handle, session = self.start_recorded(seconds=30)
        self.quit_and_wait(handle, 'running')
        mock = self.service.transport.state
        mock.job_seconds = time.monotonic() - mock.jobs[handle]['started'] + 1.5  # Over 1.5 s after it reopens.
        reopened = self.relaunch(session['id'])
        self.assertEqual(self.cards(reopened), [])
        self.assertEqual([row['id'] for row in cloud_jobs.ui_rows()], [handle], 'its running row is back')
        deadline = time.monotonic() + 10
        while not self.cards(reopened) and time.monotonic() < deadline:
            self.pump()
            time.sleep(.02)
        [card] = self.cards(reopened)
        self.assertEqual(card['title'], 'Image generation finished')

    def recorded_render(self):
        """A render's outputs as the live service returns them for contract 0.10 (f0's media, 02:22 UTC): its frames,
        three preview_<frame>.jpg (meta.preview), animation.mp4 and its report, downloaded here."""
        folder = Path(self.directory.name) / 'download'
        folder.mkdir(exist_ok=True)
        outputs = []
        rows = [(f'frame_{i:04d}.png', 'render_frames', 'image/png', {'frame': i, 'width': 3840, 'height': 2160})
                for i in (1, 2, 3)]
        rows += [(f'preview_{i:04d}.jpg', 'image', 'image/jpeg', {'frame': i, 'preview': True, 'size': 9})
                 for i in (1, 2, 3)]
        rows += [('animation.mp4', 'video', 'video/mp4', {'frames': 3, 'fps': 24}),
                 ('qa_report.json', 'report', 'application/json', {})]
        for name, kind, mime, meta in rows:
            (folder / name).write_bytes(name.encode())
            outputs.append({'name': name, 'type': kind, 'mime': mime, 'meta': meta, 'path': str(folder / name)})
        return {'id': 'job_0123456789abcdef', 'status': 'succeeded', 'outputs': outputs,
                'provenance': {'job_id': 'job_0123456789abcdef'}}

    def fake_sequencer(self):
        class Strip(dict):
            def __init__(self, name, path, kind):
                super().__init__()
                self.name, self.channel, self.kind, self.paths = name, 1, kind, [path]
                self.elements = types.SimpleNamespace(append=self.paths.append)

        class Strips(list):
            def new_image(self, name, path, channel, frame_start):
                self.append(Strip(name, path, 'IMAGE'))
                return self[-1]

            def new_movie(self, name, path, channel, frame_start):
                self.append(Strip(name, path, 'MOVIE'))
                return self[-1]
        strips = Strips()
        self.scene.sequence_editor_create = lambda: types.SimpleNamespace(strips=strips)
        return strips

    def test_render_previews_become_the_cards_thumbnails_by_their_frame(self):
        session = state.session()
        session['items'].append(state.item_user('render it'))
        job = self.recorded_render()
        saved = self.render(job)
        saved.update(conversation=session['id'], previews=[], applied=False)
        session['cloud_jobs'] = [saved]
        card = cloud_restore.card(saved, away=True)
        self.assertEqual(card['thumbnail_labels'], ['Frame 1', 'Frame 2', 'Frame 3'])
        with patch.object(conversations, 'store_image', side_effect=lambda s, path, holder: holder.update(
                image_path=str(conversations.root() / s['id'] / 'images' / (Path(path).name[:1] * 64 + '.png')))):
            cloud_restore._thumbnails(session, saved, card, job)
        self.assertEqual(len(card['thumbnails']), 3)
        self.assertEqual(saved['previews'], card['thumbnails'])

    def test_load_the_frames_keeps_the_frames_and_the_mp4_beside_the_blend_without_previews(self):
        """rel019-cloud asked for a 5 second video and never got it: R1 dropped animation.mp4 beside the frames.
        As in 0.2.18 it loads as a movie strip too, now kept beside the frames as animation.mp4."""
        strips = self.fake_sequencer()
        job = self.recorded_render()
        self.original_load_frames(job)
        strip, movie = strips
        self.assertEqual(strip.kind, 'IMAGE')
        self.assertEqual([Path(p).name for p in strip.paths], ['frame-000000.png', 'frame-000001.png', 'frame-000002.png'])
        self.assertEqual(strip.frame_final_duration, 3, 'no preview between the frames')
        folder = Path(self.directory.name) / 'renders' / 'cloud-0123456789ab'
        self.assertEqual(Path(strip.paths[0]).parent, folder, 'beside the .blend, never the system temp folder')
        self.assertEqual(strip.directory, '//renders/cloud-0123456789ab/')
        self.assertEqual((folder / 'frame-000001.png').read_bytes(), b'frame_0002.png')
        self.assertEqual((movie.kind, movie.name, movie.paths), ('MOVIE', 'animation.mp4',
                                                                 ['//renders/cloud-0123456789ab/animation.mp4']))
        self.assertEqual((folder / 'animation.mp4').read_bytes(), b'animation.mp4')
        self.assertEqual(job['video_path'], str(folder / 'animation.mp4'))
        self.assertEqual(cloud_jobs.kept_video(job['id']), folder / 'animation.mp4')
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ['animation.mp4', 'frame-000000.png', 'frame-000001.png', 'frame-000002.png'], 'no previews')

    def test_the_models_result_names_the_frames_strip_and_the_saved_mp4(self):
        self.fake_sequencer()
        self.replace(cloud_jobs, '_load_frames', self.original_load_frames)
        job = self.recorded_render()
        job['outputs'] = [{**o, 'sha256': '0' * 64, 'size': len(o['name'])} for o in job['outputs'] if o['type'] != 'report']
        job.update(tool='render', error=None, credits={'reserved': 40, 'charged': 30, 'refunded': 10},
                   progress={'pct': 100, 'message': 'Done'},
                   provenance={'job_id': job['id'], 'tool': 'render', 'kind': 'assist', 'vendor': 'loopcut',
                               'model': 'cycles', 'created_at': '2026-10-26T09:00:00Z'})
        text = cloud_jobs.apply_result({'name': 'render', 'apply': 'load_frames'}, job)
        folder = Path(self.directory.name) / 'renders' / 'cloud-0123456789ab'
        self.assertTrue(text.startswith(f"Loaded frames in the sequencer: Loopcut {job['id']} frames, animation.mp4."), text)
        self.assertIn(f"The MP4 is saved at {folder / 'animation.mp4'}", text)

    def test_frames_alone_load_as_before_and_their_card_still_offers_make_the_mp4(self):
        strips = self.fake_sequencer()
        job = self.recorded_render()
        job['outputs'] = [o for o in job['outputs'] if o['type'] != 'video']
        self.original_load_frames(job)
        self.assertEqual([s.kind for s in strips], ['IMAGE'])
        self.assertNotIn('video_path', job)
        self.assertIsNone(cloud_jobs.kept_video(job['id']))
        saved = self.render({**job, 'credits': {'charged': 30}})
        self.assertFalse(saved['video'])
        self.assertEqual([a['label'] for a in cloud_restore.card(saved, away=False)['actions']],
                         ['Make the MP4', 'Load the frames'])

    def test_a_render_that_returned_an_mp4_offers_to_open_it_instead_of_making_one(self):
        saved = self.render({**self.recorded_render(), 'credits': {'charged': 30}})
        self.assertTrue(saved['video'])
        self.assertEqual([a['label'] for a in cloud_restore.card(saved, away=False)['actions']],
                         ['Open the MP4', 'Load the frames'])
        expired = self.recorded_render()
        expired['outputs'] = [{**o, 'expired': True} if o['type'] == 'video' else o for o in expired['outputs']]
        self.assertFalse(self.render(expired)['video'], 'no MP4 to open once it is past retention')

    def test_open_the_mp4_loads_the_frames_first_then_opens_the_copy_beside_them(self):
        strips = self.fake_sequencer()
        opened = []
        self.fake.ops = types.SimpleNamespace(wm=types.SimpleNamespace(
            path_open=lambda filepath: opened.append(filepath) or {'FINISHED'}))
        session = state.session()
        job = self.recorded_render()
        saved = self.render({**job, 'credits': {'charged': 30}})
        saved.update(id=job['id'], conversation=session['id'], previews=[], applied=False)
        card = cloud_restore.card(saved, away=False)
        session['items'].append(card)
        applies = []

        def job_status(arguments, here=False):  # job_status's apply, as Load does it (cloud_restore.load).
            applies.append(arguments['job_id'])
            self.original_load_frames(self.recorded_render())
            return tools.ToolResult('Loaded frames in the sequencer.')
        self.replace(cloud, '_job_status', job_status)
        self.press(session, card, 'Open the MP4')
        folder = Path(self.directory.name) / 'renders' / 'cloud-0123456789ab'
        self.assertEqual(applies, [job['id']], 'the frames and the MP4 load first')
        self.assertEqual(opened, [str(folder / 'animation.mp4')])
        self.assertEqual(card['resolved'], 'Loaded 3 frames and the MP4 in the Video Sequencer.')
        self.assertEqual(card['error'], '')
        self.press(session, card, 'Open the MP4')  # Still there once loaded: opens it again, loads nothing.
        self.assertEqual(applies, [job['id']])
        self.assertEqual(len(opened), 2)
        self.assertEqual([s.kind for s in strips], ['IMAGE', 'MOVIE'])
        from loopcut.ui import layout
        drawn = layout.build(session, 420, 700, 1.0, lambda font, size, text: len(text) * size * 0.6, 'fast', None,
                             {'selection': [], 'context_budget': 24_000, 'account': None})
        n = session['items'].index(card)
        self.assertEqual([h['action'] for h in drawn['hits'] if h['action'][0] == 'cloud_back'], [('cloud_back', (n, 0))],
                         'Open the MP4 stays; Load is done')

    def test_an_unsaved_files_frames_go_in_loopcuts_data_folder(self):
        strips = self.fake_sequencer()
        self.data.filepath = ''
        self.original_load_frames(self.recorded_render())
        strip, movie = strips
        folder = Path(os.environ['LOOPCUT_DATA_DIR']) / 'renders' / 'cloud-0123456789ab'
        self.assertEqual(Path(strip.paths[0]).parent, folder)
        self.assertFalse(hasattr(strip, 'directory'))
        self.assertEqual(movie.paths, [str(folder / 'animation.mp4')], 'absolute, as the frames are')

    def test_a_video_alone_still_loads_as_a_movie(self):
        strips = self.fake_sequencer()
        job = self.recorded_render()
        job['outputs'] = [o for o in job['outputs'] if o['type'] == 'video']
        self.original_load_frames(job)
        self.assertEqual([s.kind for s in strips], ['MOVIE'])
        self.assertEqual(Path(strips[0].paths[0]).name, 'video-00.mp4')
        self.assertNotIn('video_path', job)

    def test_the_card_draws_its_title_frames_and_buttons(self):
        from loopcut.ui import layout
        session = state.new_session()
        card = state.item_cloud_back('job_r', 'succeeded', 'Your cloud render finished while you were away',
                                     '1,800 frames · 4K · took 1 h 52 min · 640 credits charged',
                                     [{'label': 'Make the MP4', 'do': 'mp4'}, {'label': 'Load the frames', 'do': 'load'}], 1800)
        card['thumbnails'], card['thumbnail_labels'] = ['/tmp/a.png', '/tmp/b.png'], ['Frame 1', 'Frame 1,800']
        session['items'] = [card]
        drawn = layout.build(session, 420, 700, 1.0, lambda font, size, text: len(text) * size * 0.6, 'fast', None,
                             {'selection': [], 'context_budget': 24_000, 'account': None})
        texts = ' '.join(p.get('text', '') for p in drawn['prims'])
        for words in ('Your cloud render', 'away', '640 credits charged', 'Make the MP4', 'Load the frames', 'Frame 1,800'):
            self.assertIn(words, texts)
        self.assertEqual([p['path'] for p in drawn['prims'] if p['t'] == 'image'], ['/tmp/a.png', '/tmp/b.png'])
        self.assertEqual(sorted(h['action'] for h in drawn['hits'] if h['action'][0] == 'cloud_back'),
                         [('cloud_back', (0, 0)), ('cloud_back', (0, 1))])


class LiveFinishedCardTest(RestartBase):
    """rel019-cloud (2026-10-26): a long render followed here finished while its chat was open, and its progress row
    just went, with no card and no previews, until the user asked where it was. Its card now lands as a picked-up
    job's does (cloud_restore._over), once, with one load whichever of the card or job_status applies first."""

    def follower(self):
        [thread] = [t for t in threading.enumerate() if t.name == 'Loopcut cloud job']
        return thread

    def wait_cards(self, session, count=1):
        deadline = time.monotonic() + 10
        while len(self.cards(session)) < count and time.monotonic() < deadline:
            self.pump()
            time.sleep(.02)
        self.settle_threads()
        return self.cards(session)

    def as_render(self, session, handle):
        """The image job dressed as a render (the mock renders no frames): its record says render, and its download
        brings three frames and two meta.preview JPEGs, as contract 0.10's render does."""
        saved = next(r for r in session['cloud_jobs'] if r['id'] == handle)
        saved.update(tool='render', title='Render scene', frames=3)
        original = cloud_jobs._download

        def download(client, job, cancel):
            result = original(client, job, cancel)
            folder = Path(tempfile.mkdtemp(prefix=job['id'] + '-', dir=self.directory.name))
            for name, kind, mime, meta in ([(f'frame_{i:04d}.png', 'render_frames', 'image/png', {'frame': i}) for i in (1, 2, 3)]
                                           + [(f'preview_{i:04d}.jpg', 'image', 'image/jpeg', {'frame': i, 'preview': True})
                                              for i in (1, 3)]):
                (folder / name).write_bytes(name.encode())
                result['outputs'].append({'name': name, 'type': kind, 'mime': mime, 'meta': meta, 'sha256': '0' * 64,
                                          'size': len(name), 'path': str(folder / name)})
            return result
        self.replace(cloud_jobs, '_download', download)
        self.replace(conversations, 'store_image', lambda s, path, holder: holder.update(
            image_path=str(conversations.root() / s['id'] / 'images' / (Path(path).name[:1] * 64 + '.png'))))
        return saved

    def test_a_render_followed_here_that_finishes_while_its_chat_is_open_gets_one_card_with_its_previews(self):
        handle, session = self.start_recorded(seconds=30)
        self.as_render(session, handle)
        mock = self.service.transport.state
        mock.job_seconds = time.monotonic() - mock.jobs[handle]['started'] + .5
        [card] = self.wait_cards(session)
        self.assertEqual(card['title'], 'Your cloud render finished')
        self.assertEqual([a['label'] for a in card['actions']], ['Make the MP4', 'Load the frames'])
        self.assertEqual(card['thumbnail_labels'], ['Frame 1', 'Frame 3'])
        self.assertEqual(len(card['thumbnails']), 2, 'the previews this Blender downloaded, with no second fetch')
        self.assertEqual(cloud_jobs.ui_rows(), [], 'its progress row gave way to the card')
        self.assertEqual(self.applied, [], 'nothing loads until asked')
        cloud_restore.pickup(session)  # Opening it again here adds nothing.
        self.settle_threads()
        self.assertEqual(len(self.cards(session)), 1)

    def test_the_card_shows_first_then_the_models_job_status_loads_once_and_the_card_says_so(self):
        handle, session = self.start_recorded()
        [card] = self.wait_cards(session)
        self.assertEqual((card['title'], [a['label'] for a in card['actions']]),
                         ('Image generation finished', ['Load the result']))
        self.assertTrue(self.execute('job_status', {'job_id': handle}).ok)
        self.assertEqual(self.applied, [handle])
        self.assertEqual(card['resolved'], 'Loaded into your scene.', 'no second Load on offer')
        cloud_restore.act(session, session['items'].index(card), 0)
        self.assertTrue(self.execute('job_status', {'job_id': handle}).ok)
        self.assertEqual(self.applied, [handle])

    def test_the_cards_load_first_then_job_status_loads_nothing_again(self):
        handle, session = self.start_recorded()
        [card] = self.wait_cards(session)
        self.press(session, card, 'Load the result')
        self.wait_card(card)
        self.assertEqual(card['resolved'], 'Loaded into your scene.')
        self.assertTrue(self.execute('job_status', {'job_id': handle}).ok)
        self.assertEqual(self.applied, [handle])

    def test_job_status_applies_before_the_finish_lands_and_no_card_offers_a_second_load(self):
        handle, session = self.start_recorded()
        follower = self.follower()
        follower.join(timeout=10)  # Over here, its card's landing queued for the main thread and not yet run.
        self.assertFalse(follower.is_alive())
        held = []
        while not mainthread._jobs.empty():
            held.append(mainthread._jobs.get_nowait())
        self.assertTrue(held)
        self.assertTrue(self.execute('job_status', {'job_id': handle}).ok)
        self.assertEqual(self.applied, [handle])
        for fn, future in held:
            if future.set_running_or_notify_cancel():
                future.set_result(fn())
        self.settle_threads()
        self.assertEqual(self.cards(session), [])
        self.assertEqual(self.applied, [handle])

    def test_a_job_that_finishes_while_another_chat_is_open_gets_its_card_when_its_chat_opens(self):
        handle, session = self.start_recorded(seconds=30)
        other = state.new_session()
        state.replace(other)
        mock = self.service.transport.state
        mock.job_seconds = time.monotonic() - mock.jobs[handle]['started'] + .3
        self.follower().join(timeout=10)
        self.settle_threads()
        self.assertEqual((self.cards(other), self.cards(session)), ([], []), 'no card now')
        back = conversations.load(session['id'])
        state.replace(back)
        cloud_restore.pickup(back)
        self.settle_threads()
        [card] = self.cards(back)
        self.assertEqual(card['title'], 'Image generation finished while you were away')
        self.press(back, card, 'Load the result')
        self.wait_card(card)
        self.assertTrue(self.execute('job_status', {'job_id': handle}).ok)
        self.assertEqual(self.applied, [handle])

    def test_a_failed_job_says_so_once(self):
        handle, session = self.start_recorded(seconds=30)
        self.control(fail_jobs=True)
        mock = self.service.transport.state
        mock.job_seconds = time.monotonic() - mock.jobs[handle]['started'] + .3
        [card] = self.wait_cards(session)
        self.assertEqual((card['status'], card['actions']), ('failed', []))
        self.assertTrue(next(r for r in session['cloud_jobs'] if r['id'] == handle)['seen'])
        self.assertEqual(self.cards(self.relaunch(session['id'])), [], 'said once')

    def test_a_cancel_asked_here_gets_no_second_word_beside_job_status(self):
        handle, session = self.start_recorded(seconds=30)
        self.execute('job_status', {'job_id': handle, 'cancel': True})
        self.follower().join(timeout=10)
        self.settle_threads()
        self.assertEqual(self.mock_job(handle)['status'], 'canceled')
        self.assertEqual(self.cards(session), [])
        self.assertEqual(self.cards(self.relaunch(session['id'])), [], 'nor on reopening')


class SafeToCloseTest(RestartBase):
    """Item 3 (R4): the running row of a job its chat picks up after a restart says Blender may be closed."""

    def test_a_recorded_long_job_says_it_is_safe_to_close_blender_while_it_runs(self):
        handle, session = self.start_recorded(seconds=30)
        [row] = cloud_jobs.ui_rows()
        self.assertEqual(row['safe'], 'You can quit Blender and turn your computer off: the job keeps going. '
                                      'Open this chat again and the result is here.')
        cloud_jobs.cancel_job(handle)
        self.assertNotIn('safe', cloud_jobs.ui_rows()[0], 'not while it is being canceled')

    def test_only_a_running_job_kept_with_its_chat_says_so_and_a_render_says_frames(self):
        job = {'id': 'job_r', 'tool': 'render', 'status': 'running', 'progress': {'pct': 40, 'message': 'Rendering'},
               'credits': {'reserved': 640, 'charged': 0, 'refunded': 0}, 'outputs': []}
        rows = cloud_jobs.progress_rows({'job_r': {'title': 'Render scene', 'job': job, 'resumable': True}})
        self.assertEqual(rows[0]['safe'], 'You can quit Blender and turn your computer off: the render keeps going. '
                                          'Open this chat again and the frames are here.')
        self.assertNotIn('safe', cloud_jobs.progress_rows({'job_r': {'title': 'Render scene', 'job': job}})[0])
        downloading = {**job, 'status': 'succeeded'}
        self.assertNotIn('safe', cloud_jobs.progress_rows(
            {'job_r': {'title': 'Render scene', 'job': downloading, 'resumable': True, 'ready': False}})[0])

    def test_the_row_draws_the_green_safe_to_close_note(self):
        from loopcut.ui import layout
        row = {'id': 'job_r', 'title': 'Render scene', 'pct': 40, 'message': 'Rendering', 'cancelable': True,
               'note': '640 credits held. Cancel returns them.', 'safe': cloud_jobs.SAFE_RENDER}
        drawn = layout.build(state.new_session(), 420, 700, 1.0, lambda font, size, text: len(text) * size * 0.6,
                             'fast', None, {'selection': [], 'context_budget': 24_000, 'account': None, 'cloud_jobs': [row]})
        texts = ' '.join(p.get('text', '') for p in drawn['prims'])
        self.assertIn('Safe to close', texts)
        self.assertIn('the render keeps going', texts)
        box = next(p for p in drawn['prims'] if p['id'] == 'cloud.job_r.safe.card')
        self.assertEqual((box['color'], box['border_color']), (layout.FREE_BG, layout.FREE_BORDER))


if __name__ == '__main__':
    unittest.main()
