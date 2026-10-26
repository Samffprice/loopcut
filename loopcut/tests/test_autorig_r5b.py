"""Owner copy contract over the real chat and Jobs render paths."""
import sys
import types
from pathlib import Path
import unittest
from unittest.mock import patch
from test_cloud_account import display
from test_autorig import descriptor
from loopcut import agent, autorig, cloud, cloud_account, cloud_http, cloud_jobs, rig_experimental, rig_text, state
from test_autorig_r10 import PLATFORM_NOTICES

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))
from autorig_card_check import check


class ShortCards(unittest.TestCase):
    def test_guard_rejects_old_copy_and_infrastructure_names(self):
        for text in ['One. Two. Three.', 'Uses RunPod.', 'Waiting for a GPU.', 'A worker stores this.',
                     'Temporary storage.', 'Upload hashes: private']:
            with self.subTest(text=text), self.assertRaises(AssertionError):
                check([{'id':'card.message', 'text':text}], 'card.')
        check([{'id':'card.price', 'text':'20 credits ($0.20). Next step.'}], 'card.')

    def test_real_approval_render_keeps_price_balance_action_and_one_warning(self):
        for kind in ['cost', 'budget', 'burst', 'daily_limit_reached', 'rate_limited']:
            for notice in [rig_text.QUOTE, autorig.rig_inspect.CLOTHING_WARNING]:
                with self.subTest(kind=kind, notice=notice):
                    session, turn = state.new_session(), agent.Turn()
                    card = state.item_tool(cloud.PREFIX+'auto_rig', 'Rig your character', '')
                    session['items'].append(card)
                    consent = agent._Consent(session, turn, card)
                    details = dict(text='20 credits', credits=20, balance=1000, threshold=0, required=True,
                                   rig_notice=notice)
                    def verify(_):
                        panel = display(session)
                        text = check(panel['prims'], 'item0.tool.')
                        self.assertIn('20 credits ($0.20)', text)
                        self.assertIn('Balance after: 980', text)
                        self.assertIn(('approve', 0), [h['action'] for h in panel['hits']])
                        if kind == 'cost': self.assertIn(notice, text)
                        self.assertNotIn('Every paid call', text)
                        return True
                    with patch.object(agent, '_redraw'), patch.object(agent, '_wait_for_approval', side_effect=verify):
                        # Normal authorization initializes the exact quote and account balance.
                        if kind == 'cost': consent(details)
                        else:
                            with patch.object(agent, '_wait_for_approval', return_value=True): consent(details)
                            consent._ask(kind, details)

    def test_all_progress_result_refusal_and_refund_cards(self):
        from test_rig_refund import RefundReceipts, Service, receipt
        messages = [rig_experimental.result_message(33),
                    'Standard rig added: 28 bones; try a free test pose or ask for a walk.']
        for code in ['triangle_limit', 'invalid_mesh', 'already_rigged', 'age_confirmation_required',
                     'vendor_failed', 'connection_failed', 'canceled', 'validation_failed', 'quote_changed']:
            messages.append(cloud.error_text(cloud_http.CloudError('RunPod GPU storage worker', code,
                extra={'upload_hashes': 'secret'}), rig=True))
        # Real asynchronous refund state machine, including review and an unconfirmed report.
        for response in [receipt(), receipt(status='review', review_reason='late', credits_refunded=0),
                         receipt(status='review', review_reason='limit', credits_refunded=0)]:
            service, report = RefundReceipts().make(Service(response))
            service.release.clear()
            report.start()
            messages.append(report.snapshot()['message'])
            service.release.set()
            messages.append(report.wait()['message'])
        service, report = RefundReceipts().make()
        service.failures = 2
        report.start(); messages.append(report.wait()['message'])
        for status, message in [('running', s) for s in rig_text.STAGES.values()] + [
                (s,m) for s in ['done','failed','rejected'] for m in messages]:
            for expanded in [False, True]:
                with self.subTest(status=status, message=message, expanded=expanded):
                    session = state.new_session()
                    card = state.item_tool(cloud.PREFIX+'auto_rig', 'Rig your character', '')
                    card.update(status=status, expanded=expanded, cost=rig_text.price(20,1000),
                                output=message, rig_stage=message, cloud_error={'message':'Duplicate error.'})
                    session['items'].append(card)
                    text = check(display(session)['prims'], 'item0.tool.')
                    self.assertNotIn('Duplicate error',text)
                    if status == 'running': self.assertIn(rig_text.TIME_HINT, text)

    def test_jobs_copy_budget_and_no_raw_progress(self):
        from test_rig_refund import rig_job
        for status in ['queued','running','succeeded']:
            for cancel in [False,True]:
                job = rig_job(); job.update(status=status, progress={'pct':30,'message':'RunPod GPU worker'})
                row = {'job':job,'title':'Rig your character','ready':False,'canceling':cancel}
                rows = cloud_jobs.progress_rows({'job_test':row})
                text = check(display(cloud_jobs=rows)['prims'], 'cloud.job_test.')
                self.assertIn(rig_text.TIME_HINT, text)
                self.assertIn('credits', text)

    def test_quote_cache_accepts_new_contract_without_extra_notice_fields(self):
        import autorig_contract, platform_contract
        contract = autorig_contract.Contract(platform_contract.require())
        quote = contract.fixture('quote/auto_rig-v3.json')
        self.assertEqual(set(quote), {'quote_id','credits','expires_at','experimental_notice'})
        self.assertIn(quote['experimental_notice'], PLATFORM_NOTICES + (rig_text.QUOTE,))
        self.assertEqual(cloud_account.approval_details(descriptor(), quote)['credits'],20)

    def test_plain_errors_keep_their_recovery_links(self):
        from loopcut import config
        for code, extra, action in [
                ('insufficient_credits', {'upgrade':'https://loopcut.org/credits','balance':0,'needed':20},
                 ('cloud_upgrade',0))]:
            session, turn = state.new_session(), agent.Turn()
            card = state.item_tool(cloud.PREFIX+'auto_rig', 'Rig your character', '')
            session['items'].append(card)
            with patch.object(agent,'_redraw'), patch.object(config,'billing_open',return_value=True), \
                    cloud_account.approval_context(agent._Consent(session,turn,card)):
                card.update(status='failed', expanded=True,
                    output=cloud.error_text(cloud_http.CloudError('Private backend detail',code,extra=extra),rig=True))
                panel = display(session)
            check(panel['prims'],'item0.tool.')
            self.assertIn(action, [h['action'] for h in panel['hits']])
