"""The agent entry point with the real runtime, jobs, HTTP mock and result validation.

Only Blender's image/model creation and scene diff are replaced. Requests, file resolution,
uploads, SSE, downloads, cancellation, job_status and provenance stamping run unchanged.
"""

import hashlib
import io
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
sys.modules.setdefault('bpy', types.ModuleType('bpy'))
from loopcut import (blobs, bundles, checkpoints, cloud, cloud_account, cloud_http, cloud_jobs, credentials, files,
                     mainthread, scene_diff, state, tools)
from loopcut.byok import runner
from mock_service import MockService


class Reply(io.BytesIO):
    def __init__(self, payload, status):
        super().__init__(json.dumps(payload).encode())
        self.status, self.headers = status, {}


DAILY_LIMIT = {'error': {'code': 'daily_limit_reached', 'type': 'limit_error', 'confirm_token': 'ct_test',
                         'message': "Today's cloud spending limit of 100 credits is reached."}}


class Blocks(list):
    def get(self, name):
        return next((block for block in self if block.name == name), None)


class Block(dict):
    def __init__(self, name):
        super().__init__()
        self.name = name


ADD_IMAGES = cloud_jobs._add_images  # The real apply of image outputs; setUp stands in for it.
from loopcut import conversations as _conversations  # noqa: E402
SAVE, LOAD = _conversations.save, _conversations.load  # The real ones; make_picture stands in for save.


class Image(Block):
    """Enough of an image datablock for _add_images and for resolve_files' exported copy."""
    def __init__(self, name, source=''):
        super().__init__(name)
        self.source, self.size, self.filepath_raw, self.file_format = source, (4, 4), source, 'PNG'

    def pack(self):
        pass

    def copy(self):
        return Image(self.name + '.copy', self.source)

    def save(self):
        Path(self.filepath_raw).write_bytes(Path(self.source).read_bytes())


class Images(Blocks):
    def load(self, path, check_existing=True):
        self.append(Image(Path(path).name, path))
        return self[-1]

    def remove(self, image):
        self[:] = [i for i in self if i is not image]


class Objects(Blocks):
    def new(self, name, data):
        return Block(name)


def uuid_hex(n):
    return f'{n:032x}'


class CloudEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = MockService('--live-tools', 'image_generate,image_to_3d,render,sfx', '--job-seconds', '.3')
        cls.service.__enter__()
        cls.addClassCleanup(cls.service.__exit__, None, None, None)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.main = threading.get_ident()
        self.applied, self.requests, self.progress_threads = [], [], []
        self.headers, self.refusals = [], []
        self.data = types.SimpleNamespace(images=Blocks(), objects=Blocks())
        self.fake = types.SimpleNamespace(data=self.data, path=types.SimpleNamespace(abspath=lambda value: value),
                                         app=types.SimpleNamespace(version=(5, 2, 0)))
        context = patch.dict(sys.modules, {'bpy': self.fake})
        context.start()
        self.addCleanup(context.stop)
        self.replace(scene_diff, 'snapshot', lambda: {})
        self.replace(files, 'project_roots', lambda: (Path(tempfile.gettempdir()).resolve(),))
        self.replace(cloud, '_jobs', {})
        self.replace(cloud_jobs, '_registry', {})
        self.replace(state, '_session', state.new_session())
        self.replace(cloud_jobs, '_add_images', self.import_output)
        self.replace(cloud_jobs, '_model_import', self.import_output)
        # A snapshot must not read credentials or Blender after dispatch leaves main.
        def vendor_key(name):
            self.assertEqual(threading.get_ident(), self.main)
            return 'test-vendor-key'
        self.replace(credentials, 'vendor_key', vendor_key)
        self.client = cloud_http.Client(base_url=self.service.url, api_key=self.service.key,
                                        transport=self.transport, client_headers=cloud_http.contract_headers())
        self.control(reset=True, flags={'premium_skills': False})
        self.runtime = cloud.Runtime(self.client, self.directory.name, signed_in=True)
        self.replace(cloud, '_runtime', self.runtime)
        self.join_refreshes()
        self.assertTrue(self.runtime.refresh())
        self.addCleanup(self.cleanup_jobs)

    def join_refreshes(self):
        """A charged job's apply and a server limit refresh the catalog on a thread, which saves its cache into
        this test's folder. Each one is joined before that folder is removed, so nothing writes after the test."""
        threads = []
        def refresh_async():
            def refresh():
                self.runtime.refresh()
                mainthread.request_redraw()
            threads.append(threading.Thread(target=refresh, name='loopcut-manifest', daemon=True))
            threads[-1].start()
        def join():
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), 'a catalog refresh outlived its test')
        self.replace(self.runtime, 'refresh_async', refresh_async)
        self.addCleanup(join)  # After cleanup_jobs, before the patches stop and the folder goes.

    def replace(self, owner, name, value):
        context = patch.object(owner, name, value)
        context.start()
        self.addCleanup(context.stop)

    def transport(self, request, **options):
        if request.full_url.endswith('/v1/jobs') or request.full_url.endswith('/v1/quotes'):
            self.assertNotEqual(threading.get_ident(), self.main)
            self.requests.append((request.full_url, json.loads(request.data)))
            self.headers.append({k.lower(): v for k, v in request.header_items()})
            if request.full_url.endswith('/v1/jobs') and self.refusals:
                return self.refusals.pop(0)
        return cloud_http.urllib_transport(request, **options)

    def control(self, **options):
        return self.client.request('POST', '/mock/control', options)

    def cleanup_jobs(self):
        cloud_jobs.cancel_all()
        deadline = time.monotonic() + 5
        while cloud_jobs.ui_rows() and time.monotonic() < deadline:
            self.pump()
            time.sleep(.01)
        self.assertEqual(cloud_jobs.ui_rows(), [])
        # A job's thread settles its charge after job_status has reported the job over (its watchers write the
        # retry keys and the chat's record into this test's folder): each one ends before that folder goes.
        deadline = time.monotonic() + 8
        for thread in [t for t in threading.enumerate() if t.name == 'Loopcut cloud job']:
            while thread.is_alive() and time.monotonic() < deadline:
                self.pump()
                thread.join(timeout=.05)
        for row in cloud_jobs._registry.values():
            for output in row['job']['outputs']:
                if 'path' in output:
                    shutil.rmtree(Path(output['path']).parent, ignore_errors=True)
        self.pump()

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

    def execute(self, name='image_generate', arguments=None, stopped=None, consent=lambda details: True):
        full_name = cloud.PREFIX + name
        prepared = tools.execute(full_name, json.dumps({'prompt': 'tiny prop'} if arguments is None else arguments))
        if prepared.then is None:
            return prepared
        state.session()['items'].append({'name': full_name, 'status': 'running'})
        results = []
        def work():
            try:
                with cloud_account.approval_context(consent):
                    results.append(prepared.then((stopped or threading.Event()).is_set,
                                   lambda fn: mainthread.run_on_main(fn).result(timeout=5)))
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
        self.assertEqual(threading.get_ident(), self.main)
        for output in job['outputs']:
            data = Path(output['path']).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), output['sha256'])
            self.assertEqual(len(data), output['size'])
            collection = self.data.images if output['type'] == 'image' else self.data.objects
            collection.append(Block(output['name']))
        self.applied.append(job)
        return [output['name'] for output in job['outputs']]

    def long_image(self):
        descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'image_generate')
        descriptor['latency'] = 'long'

    def pack(self, missing=(), unsupported=()):
        """Stand in for the scene snapshot and the child-Blender collect. Staging, manifest and upload are real;
        every collect writes new bytes, as a new snapshot of a live scene does."""
        steps, uploads = [], []
        def for_bundle(session):
            self.assertEqual(threading.get_ident(), self.main)
            steps.append(('snapshot', session['id']))
            return uuid_hex(len(steps))
        def bundle_from_checkpoint(session, checkpoint, staging):
            self.assertNotEqual(threading.get_ident(), self.main)
            steps.append(('collect', checkpoint))
            staging = Path(staging)
            staging.mkdir(parents=True)
            (staging / 'scene.blend').write_bytes(b'BLENDER-v502' + os.urandom(16))
            (staging / 'textures').mkdir()
            (staging / 'textures' / 'wood.png').write_bytes(b'png' * 100)
            info = {'blender': '5.2.0', 'created_at': '2026-10-17T12:00:00Z',
                    'scene': {'name': 'Scene', 'camera': 'Camera', 'frame_start': 1, 'frame_end': 3, 'fps': 24}}
            source = {'kind': 'checkpoint', 'session': session['id'], 'checkpoint': checkpoint}
            return {'manifest': bundles.manifest(staging, info, source), 'missing': list(missing),
                    'unsupported': list(unsupported), 'system': []}
        def discard(session, checkpoint):
            self.assertEqual(threading.get_ident(), self.main)
            steps.append(('discard', checkpoint))
        original = bundles.upload
        def upload(staging, manifest, *, client=None, **options):
            self.assertNotEqual(threading.get_ident(), self.main)
            self.assertEqual(set(options), {'cancel', 'progress', 'multipart'})
            self.assertEqual(options['multipart'], cloud.blob_multipart())
            uploads.append(original(staging, manifest, client=client, **options))
            return uploads[-1]
        self.replace(checkpoints, 'for_bundle', for_bundle)
        self.replace(checkpoints, 'bundle_from_checkpoint', bundle_from_checkpoint)
        self.replace(checkpoints, 'discard', discard)
        self.replace(bundles, 'upload', upload)
        return steps, uploads

    def sent(self, endpoint):
        return [payload for url, payload in self.requests if url.endswith('/v1/' + endpoint)]

    def prepared_inputs(self):
        folder = Path(self.directory.name) / 'jobs'
        return list(folder.iterdir()) if folder.is_dir() else []

    def wait_status(self, handle):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.pump()
            result = self.execute('job_status', {'job_id': handle})
            if not result.ok or not result.text.startswith('{'):
                return result
            time.sleep(.01)
        self.fail('long job never finished')

    def test_seconds_and_minutes_upload_apply_and_progress_on_correct_threads(self):
        original = cloud._progress
        def progress(name, value):
            self.progress_threads.append(threading.get_ident())
            self.assertEqual(set(value), {'pct', 'message'})
            original(name, value)
        with patch.object(cloud, '_progress', progress):
            image = self.execute()
        self.assertTrue(image.ok, image.text)
        self.assertIn('Added image references', image.text)
        self.assertTrue(self.progress_threads)
        self.assertTrue(all(thread != self.main for thread in self.progress_threads))
        self.assertIn('%', state.session()['items'][-1]['output'])
        image_job = self.applied[0]
        model = self.execute('image_to_3d', {'image': image_job['outputs'][0]['path']})
        self.assertTrue(model.ok, model.text)
        self.assertEqual(len(self.applied), 2)
        self.assertEqual(self.requests[-1][1]['input'], {})
        self.assertEqual(self.requests[-1][1]['files'], {'image': image_job['outputs'][0]['sha256']})
        for block, job in zip([self.data.images[0], self.data.objects[0]], self.applied):
            self.assertEqual(json.loads(block['loopcut_provenance']), job['provenance'])
        self.assertEqual(cloud_jobs.ui_rows(), [])

    def test_photo_to_3d_given_no_picture_of_theirs_offers_to_make_one_priced_from_the_catalog(self):
        """Owner, 2026-10-25: image_to_3d refused a chat preview, or a name that is no picture, with nothing the
        user attached: the refusal offers to make a picture first, at the catalog's prices (6 and 80 here)."""
        preview = Block('Loopcut preview')
        preview['lc_temp'] = True  # ownership.TEMP: a chat preview (ui.draw).
        self.data.images.append(preview)
        offer = ('No picture of theirs? Offer in one line, with both prices, to make one with image_generate (about '
                 '6 credits ($0.06)) from a short description (a single object on a plain background, three-quarter '
                 "view); the user then turns it into 3D with the picture's Turn it into 3D button (about 80 credits "
                 '($0.80)).')
        for image, refusal in (('Loopcut preview', "That image is Loopcut's own preview, not the user's; "
                                                   "ask them for the picture. "),
                               (str(Path(self.directory.name) / 'cat.png'), 'Scene reference or file was not found. ')):
            with self.subTest(image):
                result = self.execute('image_to_3d', {'image': image})
                self.assertFalse(result.ok)
                self.assertIn('invalid_input: ' + refusal + offer + '\n', result.text,
                              'the whole line: a refusal reaches the model cut at 400 characters')
        self.assertEqual(self.requests, [], 'nothing was quoted or sent')

    def make_picture(self):
        """image_generate with the real apply of its picture (an image datablock, its empty, a pinned reference),
        its card in the chat as the agent's loop leaves it (agent._made_pictures). Returns (card, result, image)."""
        from loopcut import agent
        self.picture_setup()
        arguments = {'prompt': 'a brass lantern, a single object on a plain background, three-quarter view'}
        result = self.execute('image_generate', arguments)
        self.assertTrue(result.ok, result.text)
        card = state.item_tool(cloud.PREFIX + 'image_generate', 'Generate image', '')
        card['status'] = 'done'
        state.session()['items'] = [card]
        call = types.SimpleNamespace(name=cloud.PREFIX + 'image_generate', arguments=json.dumps(arguments))
        agent._made_pictures(state.session(), card, call, result)
        return card, result, self.data.images[0]

    def picture_setup(self):
        """What a picture's real apply needs here: Blender's data, the chat's copies and saves stood in for."""
        from loopcut import agent, attachments, conversations
        self.data.images, self.data.objects = Images(), Objects()
        self.linked = []
        self.fake.context = types.SimpleNamespace(scene=types.SimpleNamespace(cursor=types.SimpleNamespace(
            location=(0, 0, 0))), collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=self.linked.append)))
        def attach(session, paths):  # The bounded copies for the model, without Blender's image loading.
            refs = [conversations.IMAGE_SCHEME + hashlib.sha256(f'{side}{path}'.encode()).hexdigest() + '.png'
                    for side in ('small', 'full') for path in paths]
            session['attachments'].extend({'ref': refs[n], 'full': refs[len(paths) + n], 'name': Path(path).name,
                                           'source': path} for n, path in enumerate(paths))
            return []
        self.replace(attachments, 'add', attach)
        self.replace(conversations, 'save', lambda session, **_: None)
        data = patch.dict(os.environ, {'LOOPCUT_DATA_DIR': str(Path(self.directory.name) / 'data')})
        data.start()  # The chat's own copies of its pictures (conversations.image_path).
        self.addCleanup(data.stop)
        self.replace(cloud_jobs, '_add_images', ADD_IMAGES)
        self.replace(agent, '_POLL_SECONDS', .01)

    def test_a_picture_the_model_added_unasked_and_the_user_undid_is_not_added_again_in_this_chat(self):
        """Owner, 2026-10-26 (0.2.20), undone means no: a paid output the model added without the user asking, which
        the user then undoes or deletes, is not added again in that chat: the model's next unasked call of that kind
        is refused before any quote, with no card. One the user asks for still runs."""
        from loopcut import spending
        self.picture_setup()
        state.session()['items'].append(state.item_user('model a brass lantern for my game'))
        _, consent, asks = self.asked(True, tool='image_generate')
        made = self.execute('image_generate', {'prompt': 'a brass lantern'}, consent=consent)
        self.assertTrue(made.ok, made.text)
        job = state.session()['implied'][0]['job']
        self.assertTrue(job.startswith('job_'))
        self.assertEqual(state.session()['implied'], [{'tool': 'image_generate', 'job': job, 'removed': False}])
        _, consent, asks = self.asked(True, tool='image_generate')
        kept = self.execute('image_generate', {'prompt': 'a brass lantern, closer'}, consent=consent)
        self.assertTrue(kept.ok, 'while the picture is in the file, another unasked one runs')
        self.data.images[:] = []  # Undo: the pictures and their stamps leave the file.
        _, consent, asks = self.asked(True, tool='image_generate')
        refused = self.execute('image_generate', {'prompt': 'a brass lantern again'}, consent=consent)
        self.assertFalse(refused.ok)
        self.assertTrue(refused.input_mistake, 'a plain card, not an approval')
        self.assertEqual(refused.text, "Not run: the user removed the last picture you added; don't add it again "
                                       "unless they ask. Nothing was charged.")
        self.assertEqual((asks, len(self.sent('jobs'))), ([], 2), 'nothing quoted or sent')
        self.assertTrue(state.session()['implied'][-1]['removed'])
        self.data.images[:] = [Image('Loopcut picture back', '')]  # A redo would not bring the "no" back.
        _, consent, asks = self.asked(True, tool='image_generate')
        self.assertFalse(self.execute('image_generate', {'prompt': 'a lantern'}, consent=consent).ok,
                         'remembered for the rest of this chat')
        state.session()['items'].append(state.item_user('ok, make a picture of the lantern after all'))
        _, consent, asks = self.asked(True, tool='image_generate')
        asked = self.execute('image_generate', {'prompt': 'a brass lantern'}, consent=consent)
        self.assertTrue(asked.ok, asked.text)
        self.assertEqual(len(self.sent('jobs')), 3)
        self.assertEqual(len(state.session()['implied']), 2, 'the one asked for is not an implied output')
        self.assertEqual(spending.implied_rows(state.session()['implied']), state.session()['implied'])

    def asked(self, answer=True, settings=None, tool='image_to_3d'):
        """The agent's own consent for the next call, on its own card: its asks recorded with what was sent by
        then, each answered with answer. Returns (card, consent, asks)."""
        from loopcut import agent, spending
        turn = agent.Turn()
        turn.spending = settings or spending.Settings()
        card = state.item_tool(cloud.PREFIX + tool, 'Image to 3D model', '')
        asks = []
        def wait(_):
            asks.append({**{k: card.get(k) for k in ('ask', 'ask_title', 'ask_label', 'ask_skip', 'image_path')},
                         'sent': [url.rsplit('/', 1)[-1] for url, _ in self.requests]})
            return answer
        self.replace(agent, '_wait_for_approval', wait)
        return card, agent._Consent(state.session(), turn, card), asks

    def test_a_made_picture_gets_a_turn_it_into_3d_button_at_the_catalogs_price_and_the_model_the_line(self):
        """Owner, 2026-10-26: image_generate's picture is its datablock (not TEMP, a pinned reference); its card gets
        "Turn it into 3D · about 80 credits" (image_to_3d's catalog estimate), and the model the plain line."""
        card, result, image = self.make_picture()
        self.assertEqual(result.text.split('\n')[0], f"Added image references: {image.name}. Job "
                                                     f"{image.name.split()[1]}; provenance recorded.")
        self.assertTrue(image.name.startswith('Loopcut job_'), 'the datablock name, not the file name')
        self.assertIsNone(image.get('lc_temp'))
        self.assertEqual(json.loads(image['loopcut_provenance'])['tool'], 'image_generate')
        self.assertEqual([r['name'] for r in state.session()['references']], [image.name])
        job = image.name.split()[1]
        stored = state.session()['references'][0]
        self.assertEqual(stored['made'], {'tool': 'image_generate', 'job': job}, 'Loopcut made it, even without the block')
        from loopcut import conversations
        path = str(conversations.image_path(state.session()['id'], stored['full']))
        self.assertEqual(result.made_pictures, [{'image': image.name, 'job': job, 'path': path}])
        self.assertEqual(card['to_3d'], {'pictures': [{'image': image.name, 'job': job, 'path': path,
                                                       'state': 'ready'}], 'credits': 80})
        self.assertTrue(result.model_note.endswith(cloud.TO_3D_LINE))
        self.assertEqual(cloud.TO_3D_LINE, "The picture's card has a Turn it into 3D button for the user. Before you "
                                           "turn it into 3D yourself, look at it with look_at_reference; if it isn't "
                                           "one whole object on a plain background, make it again.")
        from loopcut.ui import layout
        drawn = layout.build(state.session(), 420, 900, 1, lambda font, size, text: len(text) * size * .5, 'fast',
                             None, {})
        button = next(h for h in drawn['hits'] if h['id'] == 'item0.tool.to3d0')
        self.assertEqual(button['action'], ('to_3d', (0, 0, 80)), 'the click passes the price it shows')
        self.assertEqual(button['card'], (id(card), None), 'and the card this frame drew it on')
        self.assertEqual([p['path'] for p in drawn['prims'] if p['id'] == 'item0.tool.to3d0.picture'], [path],
                         'the picture it converts, on its card')
        self.assertIn('Turn it into 3D · about 80 credits', [p['text'] for p in drawn['prims'] if p.get('t') == 'text'])
        self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in self.requests], ['quotes', 'jobs'], 'nothing more ran')

    def look(self, name):
        """The model's look_at_reference at a picture, its pixels stubbed (no numpy here): what made_unseen reads."""
        from loopcut import conversations, tools
        for reference in state.session()['references']:  # The chat's copies, which the stub attach does not write.
            stored = conversations.image_path(state.session()['id'], reference.get('full') or reference['ref'])
            stored.parent.mkdir(parents=True, exist_ok=True)
            stored.write_bytes(b'\x89PNG')
        with patch.object(tools, '_pixels', lambda path, height=None: types.SimpleNamespace(shape=(8, 8, 4))), \
                patch.object(tools, '_save_pixels', lambda pixels, path, max_side=None: (8, 8)):
            return tools.look_at_reference(name)

    def test_the_models_3d_of_a_made_picture_it_has_not_looked_at_is_refused_with_no_card(self):
        """Owner, 2026-10-26 (0.2.20), in place of that day's always-ask card: Loopcut checks the picture itself. Not
        looked at, the model's call is refused before any quote, with the look-first text and no card; looked at, it
        runs under the usual rule, no card at the default Ask above (80 under 100)."""
        from loopcut import spending
        _, _, image = self.make_picture()
        card, consent, asks = self.asked(True)
        refused = self.execute('image_to_3d', {'image': image.name}, consent=consent)
        self.assertFalse(refused.ok)
        self.assertTrue(refused.input_mistake, 'a plain card, not an approval')
        self.assertEqual(refused.text, cloud.LOOK_FIRST.format(name=image.name))
        self.assertIn(f"Not run: you haven't looked at the picture {image.name} yet. Look at it with "
                      f'look_at_reference (name "{image.name}").', refused.text)
        self.assertLess(len(refused.text), 400, 'a refusal reaches the model cut at 400 characters')
        self.assertEqual((asks, [p['tool'] for p in self.sent('jobs')]), ([], ['image_generate']), 'nothing quoted')
        self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in self.requests], ['quotes', 'jobs'])
        looked = self.look(image.name)
        self.assertTrue(looked.ok and looked.image_path, looked.text)
        self.assertTrue(state.session()['references'][0]['made']['seen'])
        model = self.execute('image_to_3d', {'image': image.name}, consent=consent)
        self.assertTrue(model.ok, model.text)
        self.assertEqual(asks, [], 'no card: 80 is under the default Ask above')
        self.assertEqual([(p['tool'], self.runtime.quotes[p['quote_id']]['credits']) for p in self.sent('jobs')],
                         [('image_generate', 6), ('image_to_3d', 80)])
        self.assertEqual(self.sent('jobs')[1]['files'], {'image': hashlib.sha256(
            Path(image.source).read_bytes()).hexdigest()}, "the made picture's own pixels")
        self.assertEqual(state.session()['spend']['auto'], 80, 'it ran without asking')
        card, consent, asks = self.asked(True, spending.Settings(ask_above=25))
        self.assertTrue(self.execute('image_to_3d', {'image': image.name}, consent=consent).ok)
        self.assertEqual([a['ask'] for a in asks], [spending.COST], 'a user who set 25 still gets the usual card')

    def test_a_name_that_is_no_picture_gets_the_pictures_loopcut_made_not_an_offer_to_make_one(self):
        """A later turn's model may no longer have the made picture's name (the context folds older results): photo→3D
        refused with nothing the user attached names the pictures Loopcut made here."""
        _, _, image = self.make_picture()
        result = self.execute('image_to_3d', {'image': str(Path(self.directory.name) / 'lantern.png')})
        self.assertFalse(result.ok)
        self.assertIn(f'Scene reference or file was not found. Pictures Loopcut made in this chat, newest first: '
                      f'{image.name}\n', result.text)
        self.assertNotIn('No picture of theirs', result.text)

    def test_a_users_own_picture_keeps_todays_threshold(self):
        from loopcut import spending
        self.make_picture()
        own = Image('cat.png', str(Path(self.directory.name) / 'cat.png'))
        Path(own.source).write_bytes(b'\x89PNG cat')
        self.data.images.append(own)
        card, consent, asks = self.asked(True, spending.Settings(ask_above=500))
        self.assertTrue(self.execute('image_to_3d', {'image': 'cat.png'}, consent=consent).ok)
        self.assertEqual(asks, [], 'under the thresholds it runs unasked, as before')
        card, consent, asks = self.asked(True, spending.Settings(ask_above=25))
        self.assertTrue(self.execute('image_to_3d', {'image': 'cat.png'}, consent=consent).ok)
        self.assertEqual([a['ask'] for a in asks], [spending.COST], 'over Ask above it asks as before')

    def press(self, index=0, which=0, shown='button'):
        """The picture's button as the panel presses it (agent.turn_into_3d with the price it showed and the card
        it was drawn on). Returns its new card, its call running."""
        from loopcut import agent
        self.addCleanup(state.running_clicks.clear)
        items = state.session()['items']
        before = len(items)
        shown = items[index]['to_3d']['credits'] if shown == 'button' else shown
        self.assertTrue(agent.turn_into_3d(index, which, shown, drawn=(id(items[index]), None)))
        return items[before]

    def until(self, condition, seconds=8):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            self.pump()
            time.sleep(.005)
        self.pump()
        return condition()

    def click(self, index=0, which=0, shown='button'):
        """The picture's button, then the pump until its call ends. Returns the new card."""
        card = self.press(index, which, shown)
        self.assertTrue(self.until(lambda: not state.running_clicks), 'the click ran to its end')
        return card

    def test_the_pictures_button_quotes_and_runs_image_to_3d_on_it_once_and_the_model_hears_of_it(self):
        """The click is the approval: quoted, held and run on that datablock with no model call and no other card;
        a normal image_to_3d card shows it; the button then reads Turned into 3D ✓ and a second click does nothing;
        the model's next message says what happened."""
        from loopcut import agent
        picture, _, image = self.make_picture()
        model = self.click()
        self.assertEqual(model['name'], cloud.PREFIX + 'image_to_3d')
        self.assertEqual(model['status'], 'done', model.get('output'))
        self.assertIn('Imported and selected models', model['output'])
        self.assertEqual(model['cost'], 'Used 80 credits ($0.80)')
        self.assertNotIn('ask', model)
        self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in self.requests], ['quotes', 'jobs', 'quotes', 'jobs'])
        self.assertEqual(self.sent('jobs')[1]['tool'], 'image_to_3d')
        self.assertEqual(self.sent('jobs')[1]['files'], {'image': hashlib.sha256(
            Path(image.source).read_bytes()).hexdigest()})
        self.assertEqual(state.session()['spend'], {'credits': 80, 'auto': 0, 'allowed': 0}, 'approved by the click')
        self.assertEqual([p['state'] for p in picture['to_3d']['pictures']], ['done'])
        self.assertFalse(agent.turn_into_3d(0, 0), 'one conversion per picture')
        self.assertEqual(len(self.sent('jobs')), 2)
        note = state.session()['between_turns']
        self.assertEqual(len(note), 1)
        self.assertTrue(note[0].startswith(f'since your last turn: the user pressed Turn it into 3D on the picture '
                                           f'{image.name}; it ran: Imported and selected models'), note[0])
        from loopcut.ui import layout
        drawn = layout.build(state.session(), 420, 900, 1, lambda font, size, text: len(text) * size * .5, 'fast',
                             None, {})
        self.assertIn('Turned into 3D ✓', [p['text'] for p in drawn['prims'] if p.get('t') == 'text'])
        self.assertFalse([h for h in drawn['hits'] if h['id'] == 'item0.tool.to3d0'], 'no click left')

    def test_an_ask_during_a_click_is_answered_on_its_own_card_never_by_a_later_turns(self):
        """Review, 2026-10-26: a click's call has no turn of the panel's, so a server limit's ask waited forever, and
        Approve on it answered whatever card a later turn waited on. It is answered on its own card."""
        from loopcut import agent
        self.make_picture()
        self.refusals.append(Reply(DAILY_LIMIT, 402))
        card = self.press()
        self.assertTrue(self.until(lambda: card['status'] == 'awaiting'), card['status'])
        self.assertEqual(card['ask'], 'daily_limit_reached')
        later = agent.Turn()  # A turn the user started meanwhile, waiting on a card of its own.
        state.session()['turn'] = later
        items = state.session()['items']
        self.assertTrue(agent.decide(True, index=items.index(card), drawn=(id(card), card['ask_serial'])))
        self.assertTrue(self.until(lambda: not state.running_clicks))
        self.assertFalse(later.decided.is_set(), "the later turn's card was not answered")
        self.assertEqual(card['status'], 'done', card['output'])
        self.assertEqual(self.headers[-1].get('x-loopcut-confirm'), 'ct_test', 'approved past the limit, once')
        self.assertEqual(state.running_clicks, {}, 'and nothing is left running')

    def test_a_click_after_undo_took_the_pictures_datablock_runs_from_the_chats_copy(self):
        """Undo all, Ctrl+Z or a reopen takes the datablock away: the click still converts the picture, from the
        chat's own copy outside the project's folders, with no upload card (it is Loopcut's own picture)."""
        from loopcut import files
        picture, _, image = self.make_picture()
        self.replace(files, 'project_roots', lambda: (Path(self.directory.name) / 'project',))
        self.data.images.remove(image)
        card = self.click()
        self.assertEqual(card['status'], 'done', card['output'])
        self.assertEqual(self.sent('jobs')[-1]['files'], {'image': hashlib.sha256(
            Path(image.source).read_bytes()).hexdigest()})

    def test_a_made_picture_without_its_datablock_still_needs_the_models_look(self):
        """Review, 2026-10-26: without the datablock, made_picture missed it. The chat's reference says Loopcut made
        it, so the model looks at it first; then it runs with no card."""
        _, _, image = self.make_picture()
        self.data.images.remove(image)
        card, consent, asks = self.asked(True)
        consent.files_allowed = True  # Auto-run on: the chat's copy of the picture is read unasked.
        refused = self.execute('image_to_3d', {'image': image.name}, consent=consent)
        self.assertEqual(refused.text, cloud.LOOK_FIRST.format(name=image.name))
        self.look(image.name)
        self.assertTrue(self.execute('image_to_3d', {'image': image.name}, consent=consent).ok)
        self.assertEqual(asks, [])

    def asks_above(self, credits=25):
        """The user's own Ask above, which a click's quote over its button's price goes by (agent.turn_into_3d)."""
        from loopcut import config, spending
        self.replace(config, 'spending', lambda: spending.Settings(ask_above=credits))

    def test_a_quote_over_the_buttons_price_asks_on_its_card_and_skip_leaves_the_button(self):
        """The button showed 25 and the quote says 80: the click approved 25, so the card asks with the quote for a
        user whose Ask above is under it; Skip this runs nothing and the button is there again."""
        from loopcut import agent
        picture, _, _ = self.make_picture()
        self.asks_above(25)
        card = self.press(shown=25)
        self.assertTrue(self.until(lambda: card['status'] == 'awaiting'), card['status'])
        self.assertEqual((card['ask'], card['ask_skip']), ('cost', 'Skip this'))
        items = state.session()['items']
        self.assertTrue(agent.decide(False, index=items.index(card), drawn=(id(card), card['ask_serial'])))
        self.assertTrue(self.until(lambda: not state.running_clicks))
        self.assertEqual(card['status'], 'rejected')
        self.assertEqual([p['tool'] for p in self.sent('jobs')], ['image_generate'])
        self.assertEqual([p['state'] for p in picture['to_3d']['pictures']], ['ready'])
        self.assertIn('it did not run: declined: The user declined this paid call (it costs 80 credits, over the 25',
                      state.session()['between_turns'][-1])

    def test_a_reopened_chats_copy_of_a_clicks_card_answers_its_ask(self):
        """Re-review F1b, 2026-10-26: the ask's wait was found by the card dict, and a reopened chat's card is a new
        one, so Approve on it answered nothing and the click waited for good."""
        import copy
        from loopcut import agent
        self.make_picture()
        self.asks_above(25)
        old = state.session()
        card = self.press(shown=25)
        self.assertTrue(self.until(lambda: card['status'] == 'awaiting'))
        reopened = copy.deepcopy(old)  # conversations.load keeps it waiting: its click runs here.
        self.replace(state, '_session', reopened)
        mine = reopened['items'][-1]
        self.assertTrue(agent.decide(True, index=len(reopened['items']) - 1, drawn=(id(mine), mine['ask_serial'])))
        self.assertEqual(mine['status'], 'running', 'the copy shows the answer at once')
        self.assertTrue(self.until(lambda: not state.running_clicks))
        self.assertEqual(mine['status'], 'done', mine['output'])
        self.assertEqual([p['tool'] for p in self.sent('jobs')], ['image_generate', 'image_to_3d'])

    def test_a_chat_leaving_the_screen_ends_its_clicks_ask(self):
        """Re-review F1, 2026-10-26: switched away from, a chat's click ask could not be answered: lifecycle.switch_to
        says No to it, so it ends rejected with the picture ready and nothing submitted."""
        import copy
        from loopcut import conversations
        picture, _, _ = self.make_picture()
        self.asks_above(25)
        card = self.press(shown=25)
        self.assertTrue(self.until(lambda: card['status'] == 'awaiting'))
        first = state.session()
        on_disk = copy.deepcopy(first)
        self.replace(conversations, 'load', lambda conversation: on_disk)
        fake_handlers = types.SimpleNamespace(persistent=lambda function: function)
        with patch.dict(sys.modules, {'bpy.app': types.SimpleNamespace(handlers=fake_handlers),
                                      'bpy.app.handlers': fake_handlers,
                                      'loopcut.ui.host': types.SimpleNamespace(tag_redraw_all=lambda: None)}):
            sys.modules.pop('loopcut.lifecycle', None)
            from loopcut import lifecycle
            lifecycle.switch_to(state.new_session())
            sys.modules.pop('loopcut.lifecycle', None)
        self.assertTrue(self.until(lambda: not state.running_clicks))
        self.assertEqual(card['status'], 'rejected')
        self.assertEqual([p['state'] for p in on_disk['items'][0]['to_3d']['pictures']], ['ready'])
        self.assertEqual([p['tool'] for p in self.sent('jobs')], ['image_generate'], 'nothing submitted')

    def test_a_restart_mid_conversion_brings_the_button_back_and_a_click_replays_the_same_job(self):
        """Re-review F2, 2026-10-26: saved running at the click, the picture stayed "Turning into 3D…" after a quit
        or crash, though its job was submitted and charged. Loaded, it is ready, and a click sends the same job again
        by its retry key: no second charge."""
        import copy
        from loopcut import agent
        picture, _, _ = self.make_picture()
        self.replace(_conversations, 'save', SAVE)
        self.replace(_conversations, 'load', LOAD)
        session = state.session()
        with patch.object(cloud_account, 'complete', lambda *args, **kwargs: None):  # Quit before it settled.
            card = self.click()
        self.assertEqual(card['status'], 'done', card['output'])
        first = self.sent('jobs')[-1]
        balance = self.client.request('GET', '/mock/state')['balance']
        crashed = copy.deepcopy(session)  # The file as it was at the click.
        crashed['items'][0]['to_3d']['pictures'][0]['state'] = 'running'
        crashed['items'][-1].update(status='running', click=card.get('click') or [session['id'], picture['to_3d'][
            'pictures'][0]['job']])
        SAVE(crashed)
        state.running_clicks.clear()  # A new Blender: no click runs.
        loaded = LOAD(session['id'])
        self.assertEqual([p['state'] for p in loaded['items'][0]['to_3d']['pictures']], ['ready'])
        self.replace(state, '_session', loaded)
        again = self.click()
        self.assertEqual(again['status'], 'done', again['output'])
        replay = self.sent('jobs')[-1]
        self.assertEqual((replay['tool'], replay['idempotency_key']), ('image_to_3d', first['idempotency_key']))
        self.assertEqual(self.client.request('GET', '/mock/state')['balance'], balance, 'charged once')

    def test_the_model_cannot_start_a_picture_its_click_is_converting(self):
        """Re-review F4, 2026-10-26: the same call from the model while the click ran replayed its job (charged once)
        but the chat counted 160 and imported twice. The model is told it is running."""
        from loopcut import tools
        _, _, image = self.make_picture()
        card = self.press()
        result = tools.execute(cloud.PREFIX + 'image_to_3d', json.dumps({'image': image.name}))
        self.assertFalse(result.ok)
        self.assertIn("turning into 3D from its card's Turn it into 3D button now", result.text)
        self.assertTrue(self.until(lambda: not state.running_clicks))
        self.assertEqual(card['status'], 'done', card['output'])
        self.assertEqual([p['tool'] for p in self.sent('jobs')], ['image_generate', 'image_to_3d'])
        self.assertEqual(state.session()['spend']['credits'], 80)

    def test_the_models_own_3d_of_a_renamed_picture_marks_its_button(self):
        """Re-review F5, 2026-10-26: the button was marked by the picture's first name, so after a rename the model's
        own conversion left it ready and a click converted it twice."""
        from loopcut import agent, spending
        picture, _, image = self.make_picture()
        made_as = image.name
        image.name = 'Brass lantern'
        card, consent, asks = self.asked(True, spending.Settings())
        refused = self.execute('image_to_3d', {'image': 'Brass lantern'}, consent=consent)
        self.assertEqual(refused.text, cloud.LOOK_FIRST.format(name=made_as), 'the name look_at_reference knows it by')
        self.look(made_as)
        result = self.execute('image_to_3d', {'image': 'Brass lantern'}, consent=consent)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.converted, picture['to_3d']['pictures'][0]['job'])
        call = types.SimpleNamespace(name=cloud.PREFIX + 'image_to_3d', arguments=json.dumps({'image': 'Brass lantern'}))
        agent._made_pictures(state.session(), card, call, result)
        self.assertEqual([p['state'] for p in picture['to_3d']['pictures']], ['done'])
        self.assertFalse(agent.turn_into_3d(0, 0, 80))

    def test_a_model_made_while_another_file_was_open_loads_on_the_next_click_with_no_new_charge(self):
        """Re-review F6, 2026-10-26: another .blend opened mid-conversion refused the apply after the charge, and a
        click charged again. The next click loads the model the job made, from this Blender's copy of its outputs."""
        picture, _, _ = self.make_picture()
        with patch.object(cloud, '_same_scene', lambda scene: False):
            missed = self.click()
        self.assertEqual(missed['status'], 'failed')
        self.assertIn('belongs to another scene', missed['output'])
        row = picture['to_3d']['pictures'][0]
        self.assertEqual((row['state'], row['unapplied']), ('ready', self.sent('jobs')[-1] and row['unapplied']))
        self.assertTrue(row['unapplied'].startswith('job_'))
        self.assertEqual(state.session()['spend']['credits'], 80, 'charged once, when it was made')
        loaded = self.click()
        self.assertEqual(loaded['status'], 'done', loaded['output'])
        self.assertIn('Imported and selected models', loaded['output'])
        self.assertEqual(loaded['summary'], 'Image to 3D model · made already')
        self.assertEqual([p['tool'] for p in self.sent('jobs')], ['image_generate', 'image_to_3d'], 'no new job')
        self.assertEqual(state.session()['spend']['credits'], 80, 'no new charge')
        self.assertEqual((row['state'], 'unapplied' in row), ('done', False))

    def test_a_chat_reopened_or_left_while_a_click_runs_gets_its_result_and_no_second_run(self):
        """Review, 2026-10-26: reopened mid-conversion, the chat showed the button ready and a second click ran a
        second paid job, and the result went to the old dict, which was saved over the newer chat."""
        import copy
        from loopcut import agent, conversations
        picture, _, image = self.make_picture()
        saved = []
        self.replace(conversations, 'save', lambda session, **_: saved.append(session))
        old = state.session()
        card = self.press()
        self.assertIs(saved[-1], old, 'saved at the click')
        self.assertEqual([p['state'] for p in picture['to_3d']['pictures']], ['running'])
        reopened = copy.deepcopy(old)  # What conversations.load gives back (the click's card kept running).
        self.replace(state, '_session', reopened)
        self.assertFalse(agent.turn_into_3d(0, 0, 80), 'no second run')
        from loopcut.ui import layout
        drawn = layout.build(reopened, 420, 900, 1, lambda font, size, text: len(text) * size * .5, 'fast', None, {})
        self.assertIn('Turning into 3D…', [p['text'] for p in drawn['prims'] if p.get('t') == 'text'])
        self.assertFalse([h for h in drawn['hits'] if h['id'] == 'item0.tool.to3d0'])
        mark = len(saved)
        self.assertTrue(self.until(lambda: not state.running_clicks))
        own = reopened['items'][-1]
        self.assertEqual((own['status'], own['cost']), ('done', 'Used 80 credits ($0.80)'))
        self.assertNotIn('click', own)
        self.assertEqual([p['state'] for p in reopened['items'][0]['to_3d']['pictures']], ['done'])
        self.assertEqual(reopened['spend']['credits'], 80)
        self.assertEqual(len(reopened['between_turns']), 1)
        self.assertTrue(saved[mark:] and all(s is reopened for s in saved[mark:]), 'never the old dict')
        self.assertEqual(len([p for p in self.sent('jobs') if p['tool'] == 'image_to_3d']), 1)
        # Left for another chat: the result goes to its own chat's file, not the dict the click began in.
        _, _, image = self.make_picture()
        self.replace(conversations, 'save', lambda session, **_: saved.append(session))
        first = state.session()
        self.press()
        other = state.new_session()
        self.replace(state, '_session', other)
        on_disk = copy.deepcopy(first)
        self.replace(conversations, 'load', lambda conversation: on_disk if conversation == first['id'] else None)
        mark = len(saved)
        self.assertTrue(self.until(lambda: not state.running_clicks))
        self.assertEqual(on_disk['items'][-1]['status'], 'done')
        self.assertEqual(saved[mark:], [on_disk])
        self.assertEqual(other['items'], [], 'the chat on screen is not touched')

    def test_own_key_image_to_3d_takes_the_picture_as_its_reference(self):
        """Review, 2026-10-26: own-key image_to_3d takes reference, not image, so every click failed, and a model's
        own-key call on a made picture was not known as one."""
        from loopcut import tools
        _, _, image = self.make_picture()
        self.runtime.preferred = {'image_to_3d': 'byok'}
        self.runtime.keys_set = {'FAL_KEY': True}
        descriptor = next(d for d in self.runtime.descriptors() if d['name'] == 'image_to_3d')
        self.assertEqual((descriptor['backend'], cloud_jobs.picture_field(descriptor)), ('byok', 'reference'))
        self.assertEqual(cloud_jobs.made_picture(descriptor, {'reference': image.name})['name'], image.name)
        calls = []
        def execute(name, arguments):
            calls.append(json.loads(arguments))
            return tools.ToolResult('stopped here', ok=False)
        self.replace(tools, 'execute', execute)
        self.click()
        self.assertEqual(calls, [{'reference': image.name}])

    def test_a_renamed_picture_still_converts_and_a_stale_frames_click_does_nothing(self):
        from loopcut import agent
        picture, _, image = self.make_picture()
        image.name = 'Brass lantern'  # Renamed in the Outliner: the button finds it by the job that made it.
        resolved = []
        real = cloud_jobs.resolve_files
        self.replace(cloud_jobs, 'resolve_files', lambda d, a, o: resolved.append(dict(a)) or real(d, a, o))
        self.assertFalse(agent.turn_into_3d(0, 0, 80, drawn=(id(object()), None)), 'drawn on another card')
        card = self.click()
        self.assertEqual(card['status'], 'done', card['output'])
        self.assertEqual(resolved, [{'image': 'Brass lantern'}])

    def test_a_refusal_names_at_most_three_made_pictures(self):
        self.make_picture()
        references = state.session()['references']
        for n in range(5):
            references.append({**references[0], 'name': f'Loopcut job_{n} image', 'made': {'tool': 'image_generate',
                                                                                           'job': f'job_{n}'}})
        result = self.execute('image_to_3d', {'image': str(Path(self.directory.name) / 'lantern.png')})
        self.assertIn('newest first: Loopcut job_4 image, Loopcut job_3 image, Loopcut job_2 image\n', result.text)

    def test_short_of_credits_the_click_meets_the_normal_insufficient_credits_refusal(self):
        """A free account with 20 credits: the click quotes, and the job is refused for credits the usual way (the
        card hears it, as the tool paywall's pop-up does); nothing is charged and the button can be pressed again."""
        self.replace(self.runtime, 'refresh_async', lambda: None)  # The catalog as it was before the balance fell.
        picture, _, image = self.make_picture()
        self.control(plan='free', balance=20)
        model = self.click()
        self.assertEqual(model['status'], 'failed')
        self.assertIn('insufficient_credits', model['output'])
        self.assertEqual(model['cloud_error']['message'], 'Not enough credits: this needs 80 ($0.80), you have 20.')
        self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in self.requests], ['quotes', 'jobs', 'quotes', 'jobs'])
        self.assertEqual(picture['to_3d']['pictures'][0]['state'], 'ready', 'the button is there to try again')
        self.assertIn('it did not run: insufficient_credits', state.session()['between_turns'][0])
        self.assertEqual(state.session()['spend'], {'credits': 0, 'auto': 0, 'allowed': 0}, 'its hold was let go')
        # Once the catalog says so, image_to_3d is locked for credits: the button stays, and its click gets what
        # the model's own call gets (cloud.execute, where the tool paywall lane opens its pop-up).
        self.assertTrue(self.runtime.refresh())
        from loopcut import config
        rows = [{k: v for k, v in row.items() if k != 'state'} for row in picture['to_3d']['pictures']]
        self.assertIsNone(cloud.to_3d_offer(rows), 'no sales here: nothing to unlock it with')
        self.replace(config, 'billing_open', lambda: True)  # As on loopcut.org since 2026-10-16.
        self.assertEqual(cloud.to_3d_offer(rows)['credits'], 80)
        expected = tools.execute(cloud.PREFIX + 'image_to_3d', json.dumps({'image': image.name}))
        again = self.click()
        self.assertEqual((again['status'], again['output']), ('failed', expected.text))
        self.assertEqual(len(self.sent('jobs')), 2, 'nothing more was sent')

    def test_long_job_uses_one_id_and_applies_once_after_originating_turn(self):
        self.long_image()
        stopped = threading.Event()
        started = self.execute(stopped=stopped)
        self.assertTrue(started.ok, started.text)
        handle = json.loads(started.text)['job_id']
        self.assertEqual(list(cloud_jobs._registry), [handle])
        self.assertEqual([row['id'] for row in cloud_jobs.ui_rows()], [handle])
        stopped.set()  # A completed turn must not cancel its detached job.
        result = self.wait_status(handle)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.execute('job_status', {'job_id': handle}).text, result.text)
        self.assertEqual(len(self.applied), 1)

    def test_long_apply_failure_stays_failed_and_is_not_applied_twice(self):
        self.long_image()
        handle = json.loads(self.execute().text)['job_id']
        with patch.object(cloud_jobs, '_add_images', side_effect=ValueError('cannot import')) as apply:
            result = self.wait_status(handle)
            repeated = self.execute('job_status', {'job_id': handle})
        self.assertFalse(result.ok)
        self.assertFalse(repeated.ok)
        self.assertEqual(result.text, repeated.text)
        apply.assert_called_once()

    def test_long_replay_keeps_one_worker_cancel_event_and_application(self):
        self.long_image()
        arguments = {'prompt': 'same prop', 'idempotency_key': 'repeat-long-call'}
        handle = json.loads(self.execute(arguments=arguments).text)['job_id']
        cancel = cloud_jobs._registry[handle]['cancel']
        replay = self.execute(arguments=arguments)
        self.assertEqual(json.loads(replay.text)['job_id'], handle)
        self.assertIs(cloud_jobs._registry[handle]['cancel'], cancel)
        self.assertTrue(self.wait_status(handle).ok)
        self.execute(arguments=arguments)
        self.assertTrue(self.execute('job_status', {'job_id': handle}).ok)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 1)

    def test_long_status_waits_for_verified_download_and_can_cancel_it(self):
        self.long_image()
        started, release = threading.Event(), threading.Event()
        original = cloud_jobs._download
        def download(client, job, cancel):
            started.set()
            release.wait(timeout=5)
            return original(client, job, cancel)
        with patch.object(cloud_jobs, '_download', download):
            handle = json.loads(self.execute().text)['job_id']
            try:
                self.assertTrue(started.wait(timeout=3))
                status = json.loads(self.execute('job_status', {'job_id': handle}).text)
                self.assertEqual(status['status'], 'running')
                self.assertEqual(status['progress']['message'], 'Downloading outputs')
                self.assertEqual([row['id'] for row in cloud_jobs.ui_rows()], [handle])
                self.execute('job_status', {'job_id': handle, 'cancel': True})
            finally:
                release.set()
            result = self.wait_status(handle)
        self.assertFalse(result.ok)
        self.assertIn('canceled', result.text)
        self.assertEqual(self.applied, [])

    def test_server_errors_keep_codes_and_extras(self):
        for code, extra in [('insufficient_credits', 'needed'), ('generation_disabled', 'scope'),
                            ('private_mode', 'data_class'), ('vendor_failed', 'refunded')]:
            with self.subTest(code=code):
                self.control(force_error=code)
                result = self.execute()
                self.assertFalse(result.ok)
                self.assertIn(code, result.text)
                self.assertIn(extra, result.text)
        self.assertEqual(self.applied, [])

    def test_5xx_answers_after_the_job_was_made_and_charged_buy_one_picture(self):
        # rel019-lowcredit (2026-10-26): the job was made and charged, but answered with a 500, and following it a
        # database timeout answered 500 again. The model heard "failed" and bought the picture again with a new key.
        self.replace(cloud_jobs, 'SERVER_RETRY_SECONDS', (.1,) * 6)
        before = self.client.request('GET', '/mock/state')
        busy, send = {'create': 1, 'events': 1, 'poll': 2}, self.client.transport

        def transport(request, **options):
            url = request.full_url
            kind = ('create' if request.method == 'POST' and url.endswith('/v1/jobs') else
                    'events' if request.method == 'GET' and url.endswith('/events') else
                    'poll' if request.method == 'GET' and '/v1/jobs/' in url else None)
            if kind and busy[kind]:
                busy[kind] -= 1
                if kind == 'create':
                    send(request, **options).close()  # Made and charged; only its answer is lost.
                return Reply({'error': {'code': 'server_error', 'type': 'server_error', 'message':
                                        'The job service is temporarily unavailable. Try again shortly.'}}, 500)
            return send(request, **options)
        self.replace(self.client, 'transport', transport)
        result = self.execute()
        self.assertTrue(result.ok, result.text)
        self.assertEqual(busy, {'create': 0, 'events': 0, 'poll': 0}, 'every 5xx was answered')
        self.assertEqual(len(self.applied), 1)
        posts = self.sent('jobs')
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1], 'the same body and key, so the server returned its job')
        after = self.client.request('GET', '/mock/state')
        self.assertEqual(after['jobs'], before['jobs'] + 1, 'one job')
        charged = self.applied[0]['credits']['charged']
        self.assertGreater(charged, 0)
        self.assertEqual(before['balance'] - after['balance'], charged, 'charged once')

    def test_a_renders_replay_with_its_key_sends_its_first_quote_and_scene_and_gets_the_one_job(self):
        # 0.2.20: job_unconfirmed tells the model to call again with the same arguments plus the key. The replay
        # quoted anew and packed the scene anew, so its body was another request's: a second job, or a refusal.
        self.replace(cloud_jobs, 'SERVER_RETRY_SECONDS', (.01,) * 6)
        steps, uploads = self.pack()
        before = self.client.request('GET', '/mock/state')
        busy, send = {'create': 99}, self.client.transport

        def transport(request, **options):
            if busy['create'] and request.method == 'POST' and request.full_url.endswith('/v1/jobs'):
                busy['create'] -= 1
                send(request, **options).close()  # Made and charged; only its answers are lost.
                return Reply({'error': {'code': 'server_error', 'type': 'server_error', 'message': 'Busy.'}}, 500)
            return send(request, **options)
        self.replace(self.client, 'transport', transport)
        arguments = {'frame_start': 1, 'frame_end': 3}
        with patch.object(cloud_jobs, '_load_frames', self.import_output), \
                patch.object(cloud_jobs, '_reports', return_value=[]):
            first = self.execute('render', arguments)
            self.assertFalse(first.ok)
            self.assertTrue(first.text.startswith('job_unconfirmed'), first.text)
            key = self.sent('jobs')[0]['idempotency_key']
            busy['create'] = 0
            again = self.execute('render', {**arguments, 'idempotency_key': key})
            self.assertTrue(again.ok, again.text)
            result = self.wait_status(json.loads(again.text)['job_id'])
        self.assertTrue(result.ok, result.text)
        posts = self.sent('jobs')
        self.assertEqual(len(posts), 1 + len(cloud_jobs.SERVER_RETRY_SECONDS) + 1)
        self.assertTrue(all(post == posts[0] for post in posts), 'one body: the first quote, scene and key')
        self.assertEqual(len(uploads), 1, 'the scene was packed once')
        self.assertEqual(len(self.sent('quotes')), 1)
        after = self.client.request('GET', '/mock/state')
        self.assertEqual(after['jobs'], before['jobs'] + 1, 'one job')
        self.assertEqual(len(self.applied), 1)

    def test_a_replay_the_server_refuses_as_another_requests_key_is_never_a_bare_error(self):
        self.refusals = [Reply({'error': {'code': 'invalid_input', 'type': 'invalid_request', 'field': 'idempotency_key',
                                          'message': 'This idempotency_key was already used for a different request. '
                                                     'Send a new idempotency_key for a new job.'}}, 422)]
        result = self.execute('image_generate', {'prompt': 'tiny prop', 'idempotency_key': 'k-first-attempt'})
        self.assertFalse(result.ok)
        self.assertTrue(result.text.startswith('job_unconfirmed'), result.text)
        for said in ('may still be running and be charged', 'do not start another', 'job_status'):
            self.assertIn(said, result.text)
        self.assertNotIn('Send a new idempotency_key', result.text)
        self.assertEqual(len(self.sent('jobs')), 1, 'not asked again')

    def test_live_account_policy_changes_are_refused_with_stale_manifest(self):
        for setting, tool, code, extra in [('no_generation', 'image_generate', 'generation_disabled', 'account'),
                                           ('private_mode', 'sfx', 'private_mode', 'C')]:
            with self.subTest(setting=setting):
                self.control(settings={'no_generation': False, 'private_mode': False})
                self.assertTrue(self.runtime.refresh())
                self.control(settings={setting: True})
                result = self.execute(tool)
                self.assertFalse(result.ok)
                self.assertIn(code, result.text)
                self.assertIn(extra, result.text)

    def test_private_manifest_and_byok_catalog_cannot_restore_refused_tool(self):
        from loopcut.byok import catalog
        self.runtime.keys_set = {'ELEVENLABS_API_KEY': True}
        self.runtime.preferred = {'sfx': 'byok'}
        self.assertNotIn(cloud.PREFIX + 'sfx', [s['function']['name'] for s in cloud.tool_schemas()])
        self.assertIn('private_mode', self.execute('sfx').text)
        local = next(d for d in catalog.descriptors(no_generation=False, private_mode=True,
                     keys_set=self.runtime.keys_set) if d['name'] == 'sfx')
        self.assertEqual(local['unavailable_reason'], 'private_mode')

    def test_async_vendor_failure_has_refund_and_long_failure_is_retained(self):
        self.control(fail_jobs=True)
        for long in (False, True):
            if long:
                self.long_image()
            result = self.execute()
            if long:
                handle = json.loads(result.text)['job_id']
                result = self.wait_status(handle)
                self.assertFalse(self.execute('job_status', {'job_id': handle}).ok)
            self.assertFalse(result.ok)
            self.assertIn('vendor_failed', result.text)
            self.assertIn('refunded', result.text)
        self.assertEqual(self.applied, [])

    def test_failed_sound_effect_can_be_retried_even_with_its_key(self):
        # The contract keeps a key's job for good: resent with the same body it replays the failure, with
        # another body it is refused ("already used for a different request"). A retry is a new job.
        self.control(settings={'no_generation': False, 'private_mode': False}, fail_jobs=True)
        self.assertTrue(self.runtime.refresh())
        self.replace(cloud_jobs, '_add_audio', self.import_output)
        failed = self.execute('sfx', {'prompt': 'door creak'})
        self.assertFalse(failed.ok)
        self.assertIn('vendor_failed', failed.text)
        self.assertNotIn('idempotency_key', failed.text)  # Nothing to retry it with.
        key = self.requests[-1][1]['idempotency_key']
        self.control(fail_jobs=False)
        for arguments in ({'prompt': 'door creak', 'idempotency_key': key},
                          {'prompt': 'door creak, slower', 'idempotency_key': key}):
            result = self.execute('sfx', arguments)
            self.assertTrue(result.ok, result.text)
        keys = [body['idempotency_key'] for url, body in self.requests if url.endswith('/v1/jobs')]
        self.assertEqual(len(set(keys)), 3, keys)
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 3)
        self.assertEqual(len(self.applied), 2)

    def test_cancel_from_panel_and_job_status_share_the_same_event(self):
        self.long_image()
        for source in ('panel', 'tool'):
            with self.subTest(source=source):
                handle = json.loads(self.execute().text)['job_id']
                if source == 'panel':
                    self.assertTrue(cloud_jobs.cancel_job(handle))
                else:
                    self.execute('job_status', {'job_id': handle, 'cancel': True})
                self.assertTrue(cloud_jobs._registry[handle]['cancel'].is_set())
                result = self.wait_status(handle)
                self.assertFalse(result.ok)
                self.assertIn('canceled', result.text)
                self.assertIn('refunded', result.text)
        self.assertEqual(self.applied, [])

    def test_a_models_cancel_of_a_running_paid_job_asks_the_user_first(self):
        # rel019-cloud (2026-10-26): the model canceled the user's approved 29-credit render at 93% by itself.
        self.long_image()
        handle = json.loads(self.execute().text)['job_id']
        asked = []

        def keep(details):
            asked.append(details)
            return False
        kept = self.execute('job_status', {'job_id': handle, 'cancel': True}, consent=keep)
        self.assertFalse(kept.ok)
        self.assertIn('The user kept this cloud job running: it was not canceled', kept.text)
        self.assertFalse(cloud_jobs._registry[handle]['cancel'].is_set())
        [question] = [details['cancel_job'] for details in asked]
        self.assertEqual(question['title'], 'Image generation')
        self.assertGreater(question['held'], 0)
        # "Cancel it": the cancel goes, and its credits come back.
        self.execute('job_status', {'job_id': handle, 'cancel': True})
        self.assertTrue(cloud_jobs._registry[handle]['cancel'].is_set())
        result = self.wait_status(handle)
        self.assertIn('refunded', result.text)
        self.assertEqual(self.applied, [])

    def test_a_cancel_the_users_latest_message_asked_for_needs_no_card(self):
        self.long_image()
        for said, asks in (("why did it stop?", True), ("stop that render please", False)):
            with self.subTest(said=said):
                handle = json.loads(self.execute().text)['job_id']
                state.session()['items'].append(state.item_user(said))
                asked = []
                self.execute('job_status', {'job_id': handle, 'cancel': True},
                             consent=lambda details: asked.append(details) or True)
                self.assertEqual(any('cancel_job' in details for details in asked), asks)
                self.assertTrue(cloud_jobs._registry[handle]['cancel'].is_set())
                self.wait_status(handle)

    def test_agent_stop_cancels_blocking_job_without_apply(self):
        stopped = threading.Event()
        with patch.object(cloud, '_progress', side_effect=lambda name, value: stopped.set()):
            result = self.execute(stopped=stopped)
        self.assertFalse(result.ok)
        self.assertIn('canceled', result.text)
        self.assertEqual(self.applied, [])

    def test_quote_bundle_and_retry_metadata_round_trip_through_schema_and_dispatch(self):
        descriptor = next(d for d in self.runtime.descriptors() if d['name'] == 'render')
        parameters = cloud.schema(descriptor)['function']['parameters']
        self.assertNotIn('bundle', parameters['properties'])  # Loopcut packs the scene; the model never sends one.
        self.assertIn('quote_id', parameters['properties'])
        self.assertIn('idempotency_key', parameters['properties'])
        steps, uploads = self.pack()
        arguments = {'frame_start': 1, 'frame_end': 3}
        quoted = self.execute('render', arguments, consent=lambda details: False)
        self.assertFalse(quoted.ok)
        self.assertIn('approval_required', quoted.text)
        quote = next(iter(self.runtime.quotes.values()))
        self.assertIn('credits', quote)
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)
        self.assertEqual([q['bundle'] for q in self.sent('quotes')], uploads)
        arguments.update(quote_id=quote['quote_id'], idempotency_key='test-render-retry')
        self.assertTrue(cloud.approval(cloud.PREFIX + 'render', arguments))
        self.assertIn(f"{quote['credits']} credits (${quote['credits'] / 100:.2f}), quoted",
                      cloud.describe(cloud.PREFIX + 'render', arguments)[0])
        def consent(details):
            self.assertEqual(details['credits'], quote['credits'])
            self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)
            # A new call packs the scene as it is now: the old quote was for the old bundle, so it quotes again.
            self.assertEqual([q['bundle'] for q in self.sent('quotes')], uploads)
            return True
        # Keep result validation/stamping, but avoid sequencer/report Blender APIs for render.
        with patch.object(cloud_jobs, '_load_frames', self.import_output), patch.object(cloud_jobs, '_reports', return_value=[]):
            started = self.execute('render', arguments, consent=consent)
            self.assertTrue(started.ok, started.text)
            result = self.wait_status(json.loads(started.text)['job_id'])
        self.assertTrue(result.ok, result.text)
        payload = self.sent('jobs')[-1]
        self.assertEqual(payload['input'], {'frame_start': 1, 'frame_end': 3})
        self.assertEqual(payload['bundle'], uploads[-1])
        self.assertEqual(len(uploads), 2)
        self.assertNotEqual(payload['quote_id'], quote['quote_id'])  # The stale quote is never forwarded.
        self.assertIn(payload['quote_id'], self.runtime.quotes)
        self.assertEqual(payload['idempotency_key'], 'test-render-retry')

    def test_quote_approval_and_long_handoff_complete_in_one_call(self):
        descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'render')
        descriptor['apply'] = 'none'  # Verify downloads without Blender's sequencer.
        self.pack()
        approvals = []
        def consent(details):
            self.assertEqual(len(self.runtime.quotes), 1)
            self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in self.requests], ['quotes'])
            approvals.append(details)
            return True
        stopped = threading.Event()
        started = self.execute('render', {'frame_start': 1, 'frame_end': 3}, stopped=stopped, consent=consent)
        self.assertTrue(started.ok, started.text)
        handle = json.loads(started.text)['job_id']
        self.assertEqual(list(cloud_jobs._registry), [handle])
        self.assertEqual(list(cloud._jobs), [handle])
        self.assertEqual([row['id'] for row in cloud_jobs.ui_rows()], [handle])
        self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in self.requests], ['quotes', 'jobs'])
        quote = self.runtime.quotes[self.requests[-1][1]['quote_id']]
        self.assertEqual([d['credits'] for d in approvals], [quote['credits']])
        stopped.set()
        result = self.wait_status(handle)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.execute('job_status', {'job_id': handle}).text, result.text)

    def test_render_packs_the_live_scene_once_and_quotes_and_runs_with_its_hash(self):
        steps, uploads = self.pack()
        shown, original = [], cloud._progress
        def progress(name, value):
            shown.append(value)
            original(name, value)
        cards = []
        def consent(details):
            # The quote card: the bundle is uploaded and quoted, and nothing is packed again while it waits.
            self.assertEqual(len(uploads), 1)
            self.assertEqual([q['bundle'] for q in self.sent('quotes')], uploads)
            self.assertEqual(self.sent('jobs'), [])
            cards.append(details)
            return True
        # A bundle the model made up is never sent: Loopcut packs the open scene itself.
        arguments = {'frame_start': 1, 'frame_end': 3, 'bundle': 'f' * 64}
        with patch.object(cloud, '_progress', progress), patch.object(cloud_jobs, '_load_frames', self.import_output), \
                patch.object(cloud_jobs, '_reports', return_value=[]):
            started = self.execute('render', arguments, consent=consent)
            self.assertTrue(started.ok, started.text)
            handle = json.loads(started.text)['job_id']
            result = self.wait_status(handle)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.applied[-1]['id'], handle)  # Its frames came back through the render's apply.
        session = state.session()['id']
        self.assertEqual(steps, [('snapshot', session), ('collect', uuid_hex(1)), ('discard', uuid_hex(1))])
        self.assertEqual(len(uploads), 1)
        self.assertEqual(len(cards), 1)
        self.assertEqual(self.sent('jobs')[0]['bundle'], uploads[0])
        self.assertEqual(self.sent('jobs')[0]['input'], {'frame_start': 1, 'frame_end': 3})
        self.assertIn('Packing the scene…', shown)
        self.assertTrue(any(isinstance(v, str) and v.startswith('Sending a copy of the scene (') for v in shown), shown)
        # In setup's words ("Except for a cloud render, which sends a copy of the scene and its files."), never "Scene uploaded".
        self.assertTrue(any(isinstance(v, str) and v.startswith('Sent a copy of the scene for this render (')
                            for v in shown), shown)
        self.assertEqual(self.prepared_inputs(), [])  # The staged bundle went with the call's inputs.
        image = self.execute()  # A tool without a bundle input never packs the scene.
        self.assertTrue(image.ok, image.text)
        self.assertEqual(len(steps), 3)
        self.assertNotIn('bundle', self.sent('jobs')[-1])

    def test_incomplete_scene_is_refused_before_any_upload_quote_or_job(self):
        steps, uploads = self.pack(
            missing=[{'type': 'textures', 'name': 'Wood', 'path': '//textures/wood.png'}],
            unsupported=[{'type': 'library', 'name': 'Set', 'path': '//set.blend',
                          'reason': 'Nested, missing or externally dependent linked library'}])
        consent = unittest.mock.Mock(return_value=True)
        result = self.execute('render', {'frame_start': 1}, consent=consent)
        self.assertFalse(result.ok)
        self.assertIn('bundle_incomplete', result.text)
        for name in ('Wood', '//textures/wood.png', 'Set', '//set.blend', 'externally dependent'):
            self.assertIn(name, result.text)
        consent.assert_not_called()
        self.assertEqual((uploads, self.requests), ([], []))
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)
        self.assertEqual([step[0] for step in steps], ['snapshot', 'collect', 'discard'])
        self.assertEqual(self.prepared_inputs(), [])

    def test_python_drivers_are_named_with_what_to_do_and_counted_past_ten_before_any_upload(self):
        reason = bundles.PYTHON_DRIVER
        rows = [{'type': 'driver', 'name': f'Object "Rig.{n:03}"', 'path': 'location[2]', 'reason': reason}
                for n in range(12)]
        steps, uploads = self.pack(unsupported=rows)
        consent = unittest.mock.Mock(return_value=True)
        result = self.execute('render', {'frame_start': 1}, consent=consent)
        self.assertFalse(result.ok)
        first, _, extras = result.text.partition('\n')
        self.assertIn("can't pack this scene: 12 unsupported item(s)", first)
        self.assertIn('Nothing was uploaded', first)
        named = json.loads(extras)['error']['unsupported']
        self.assertEqual(named[0], f'driver Object "Rig.000" location[2]: {reason}')
        self.assertEqual((len(named), named[-1]), (11, 'and 2 more'))
        for text in ('simple expression', 'bake'):
            self.assertIn(text, named[0])
        consent.assert_not_called()
        self.assertEqual((uploads, self.requests), ([], []))
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)

    def test_limit_card_approve_resends_the_same_bundle_without_repacking(self):
        descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'render')
        descriptor['apply'] = 'none'
        steps, uploads = self.pack()
        self.refusals.append(Reply(DAILY_LIMIT, 402))
        cards = []
        def consent(details):
            cards.append('limit' if details.get('limit') else 'cost')
            return True
        started = self.execute('render', {'frame_start': 1, 'frame_end': 3}, consent=consent)
        self.assertTrue(started.ok, started.text)
        self.assertEqual(cards, ['cost', 'limit'])
        jobs = self.sent('jobs')
        self.assertEqual([job['bundle'] for job in jobs], uploads * 2)
        self.assertEqual(jobs[0]['idempotency_key'], jobs[1]['idempotency_key'])
        self.assertEqual(self.headers[-1].get('x-loopcut-confirm'), 'ct_test')
        self.assertEqual(len(uploads), 1)
        self.assertEqual([step[0] for step in steps], ['snapshot', 'collect', 'discard'])
        self.assertTrue(self.wait_status(json.loads(started.text)['job_id']).ok)

    def test_skip_or_stop_while_packing_leaves_nothing_submitted(self):
        steps, uploads = self.pack()
        skipped = self.execute('render', {'frame_start': 1}, consent=lambda details: False)
        self.assertFalse(skipped.ok)
        self.assertIn('approval_required', skipped.text)
        self.assertEqual(len(self.sent('quotes')), 1)
        stopped, collect = threading.Event(), checkpoints.bundle_from_checkpoint
        def stop_while_collecting(*args):
            stopped.set()  # The panel's Stop, while the child Blender collects.
            return collect(*args)
        with patch.object(checkpoints, 'bundle_from_checkpoint', stop_while_collecting):
            canceled = self.execute('render', {'frame_start': 1}, stopped=stopped)
        self.assertFalse(canceled.ok)
        self.assertIn('canceled', canceled.text)
        self.assertEqual(len(uploads), 1)  # Stopped before its upload.
        self.assertEqual(len(self.sent('quotes')), 1)
        self.assertEqual(self.sent('jobs'), [])
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)
        self.assertEqual(cloud_account._pending(self.runtime), {})  # No retry key waits for a job that never was.
        self.assertEqual([step[0] for step in steps], ['snapshot', 'collect', 'discard'] * 2)
        self.assertEqual(self.prepared_inputs(), [])

    def test_byok_real_runner_and_shared_apply_with_main_thread_key_snapshot(self):
        self.runtime.preferred = {'image_generate': 'byok'}
        self.runtime.keys_set = {'OPENAI_API_KEY': True}
        # Replace only the vendor boundary. The BYOK envelope and saved output are real.
        artifact = {'name': 'image', 'mime': 'image/png', 'data': b'unit-test-image', 'meta': {}}
        with patch.object(runner.openai, 'execute', return_value=[artifact]) as vendor:
            result = self.execute()
        self.assertTrue(result.ok, result.text)
        self.assertEqual(vendor.call_args.args[-1], 'test-vendor-key')
        provenance = json.loads(self.data.images[0]['loopcut_provenance'])
        self.assertEqual(provenance['backend'], 'byok')
        self.assertEqual(self.requests, [])

    def test_canceled_byok_result_is_never_applied(self):
        self.runtime.preferred = {'image_generate': 'byok'}
        self.runtime.keys_set = {'OPENAI_API_KEY': True}
        stopped = threading.Event()
        def vendor(*args):
            stopped.set()
            return []
        with patch.object(runner.openai, 'execute', side_effect=vendor):
            result = self.execute(stopped=stopped)
        self.assertFalse(result.ok)
        self.assertIn('canceled', result.text)
        self.assertEqual(self.applied, [])
