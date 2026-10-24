"""R6: a rig's exact quote, and a worker's rig card that gets it."""
import unittest
from unittest.mock import patch

from test_spending import managed
from test_cloud_account import display
from loopcut import agent, spending, state


class PaidApproval(unittest.TestCase):
    def setUp(self):
        self.session, self.turn = state.new_session(), agent.Turn()
        self.turn.spending = spending.Settings()
        redraw = patch.object(agent, '_redraw')
        redraw.start()
        self.addCleanup(redraw.stop)

    def test_rig_then_motion_each_show_the_exact_quote(self):
        self.turn.spending = spending.Settings(ask_every=True)
        seen = []
        for name, price, threshold in [('auto_rig', 20, 0), ('motion_from_text', 16, 25)]:
            card = state.item_tool('loopcut_cloud__' + name, name, '')
            self.session['items'].append(card)
            consent = agent._Consent(self.session, self.turn, card, files_allowed=True)
            details = managed(10, server=threshold, quote={
                'quote_id': 'qt_' + name, 'credits': price, 'expires_at': 'later'})
            def approve(_):
                panel = display(self.session)
                text = ' '.join(p.get('text', '') for p in panel['prims']
                                if p.get('id', '').startswith(f'item{len(seen)}.tool.'))
                self.assertIn(str(price) + ' credits', text)
                self.assertNotIn('About 10', text)
                seen.append(card['name'])
                return True
            with patch.object(agent, '_wait_for_approval', side_effect=approve):
                consent(details)
        self.assertEqual(seen, ['loopcut_cloud__auto_rig', 'loopcut_cloud__motion_from_text'])
        self.assertEqual(self.session['spend'], {'credits': 36, 'auto': 0, 'allowed': 0})

    def test_a_workers_existing_progress_card_gets_the_quote(self):
        worker = agent.Turn()
        worker.worker, worker.chat = 'Rigging', (self.session, self.turn)
        card = state.item_tool('loopcut_cloud__auto_rig', 'Rig your character', '')
        card['status'] = 'running'
        consent = agent._Consent(state.new_session(), worker, card, asks=False)
        consent({'progress': {'pct': 5, 'message': 'Preparing your character.'}})
        consent(managed(20))
        self.assertEqual(len(self.session['items']), 1)
        self.assertIn('20 credits', self.session['items'][0]['cost'])
