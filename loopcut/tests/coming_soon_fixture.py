"""The beta manifest's eleven Coming soon services (contract 0.6, proposal 010 `display`), plus tools the
local BYOK catalog does not know. One descriptor has no `display`, as an older producer would send."""

ROWS = [  # name, display title, category (None: no display object)
    ('render', 'Cloud rendering', 'render'),
    ('image_generate', 'Image generation', 'images'),
    ('image_edit', 'Image editing', 'images'),
    ('image_to_3d', 'Image to 3D', '3d_models'),
    ('retopology', 'Retopology', '3d_models'),
    ('voice_line', 'Voice lines', 'audio'),
    ('sfx', 'Sound effects', 'audio'),
    ('dialogue_from_recordings', 'Dialogue from your recordings', 'audio'),
    ('lip_sync', 'Lip sync', 'characters'),
    ('auto_rig', 'Auto-rig', 'characters'),
    ('asset_search', 'Loopcut cloud asset library', None),
]
LABELS = ['Render', 'Images', '3D models', 'Audio', 'Characters']
# Contract 0.6's top-level data_class, the managed route's (loopcut-web release/beta 6b6600d's manifest fixtures).
# Render, lip sync and the asset library have no own-key route, so no byok.data_class. One tool is left
# without it, as a 0.5 producer would send.
DATA_CLASSES = {'render': 'A', 'image_generate': 'A', 'image_edit': 'A', 'image_to_3d': 'A', 'retopology': 'B',
                'voice_line': 'B', 'sfx': 'C', 'dialogue_from_recordings': 'C', 'lip_sync': 'A', 'asset_search': 'B'}


def manifest():
    tools = []
    for name, title, category in ROWS:
        descriptor = {
            'name': name, 'namespace': 'loopcut_cloud', 'version': 1, 'title': name.replace('_', ' '),
            'description': title, 'kind': 'gen', 'status': 'coming_soon', 'available': False,
            'unavailable_reason': 'sign_in', 'input_schema': {'type': 'object', 'properties': {}},
            'file_inputs': [], 'output': {'type': 'report', 'formats': ['json']}, 'apply': 'none',
            'cost': {'credits_estimate': 0, 'quote_required': False}, 'latency': 'seconds',
            'approval': {'confirm_over_credits': 50}, 'backends': ['managed'], 'byok': None,
        }
        if name in DATA_CLASSES:
            descriptor['data_class'] = DATA_CLASSES[name]
        if name == 'image_generate':
            descriptor.update(backends=['managed', 'byok'],
                              byok={'provider': 'openai', 'key_name': 'OPENAI_API_KEY', 'data_class': 'A'})
        if category is not None:
            descriptor['display'] = {'title': title, 'summary': title + '.', 'category': category}
        tools.append(descriptor)
    return {'contract': {'version': '0.6', 'min_client': '0.1'}, 'account': None,
            'flags': {'services': True, 'premium_skills': False}, 'tools': tools, 'skills': []}
