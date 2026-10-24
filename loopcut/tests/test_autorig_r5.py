"""Persona regressions: quote retries and one current rig card."""
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from test_cloud_account import display
from test_autorig import descriptor
from loopcut import agent, autorig, cloud, cloud_account as account, cloud_http, state


class PersonaCards(unittest.TestCase):
    def test_single_rig_card_has_current_stage_and_time(self):
        session=state.new_session()
        card=state.item_tool(cloud.PREFIX+'auto_rig','Rig your character','')
        card.update(status='running',rig_stage='Rigging your character…', cloud_job_id='job_a',
                    output='Getting a credit quote…')
        session['items'].append(card)
        row={'id':'job_a','title':'Duplicate rig title','pct':30,'message':'Working','cancelable':True,'note':''}
        panel=display(session,cloud_jobs=[row])
        text=' '.join(p.get('text','') for p in panel['prims'])
        self.assertIn('Rigging your character',text)
        self.assertIn(autorig.TIME_HINT, text)
        self.assertNotIn('Uploads the selected',text)
        self.assertNotIn('Getting a credit quote',text)
        self.assertNotIn('Duplicate rig title',text)
        self.assertIn(('cloud_job_cancel','job_a'),[h['action'] for h in panel['hits']])

    def test_result_names_are_prominent_for_standard_and_experimental(self):
        from loopcut import scene_diff
        for tier in ('standard','experimental'):
            details={'tier':tier,'armature':'Character Rig.001','meshes':['Body.001','Coat']}
            def apply(_descriptor,_job,*,details):
                details['rig']={'tier':tier,'armature':'Character Rig.001','meshes':['Body.001','Coat']}
                return 'Ready to pose.'
            with patch.object(scene_diff,'snapshot',return_value={}), patch.object(cloud,'_runtime',None):
                result=cloud._apply(types.SimpleNamespace(apply_result=apply), descriptor(), {})
            self.assertIn('exact armature object',result.model_note)
            self.assertEqual(json.loads(result.model_note.split('\n',1)[1]),details)
            self.assertEqual(result.text,'Ready to pose.')

    def test_delayed_progress_preserves_approval_and_terminal_cards(self):
        for status in ('awaiting', 'done', 'failed', 'rejected'):
            with self.subTest(status=status):
                session, turn = state.new_session(), agent.Turn()
                card = state.item_tool(cloud.PREFIX + 'auto_rig', 'Rig', '')
                card.update(status=status, rig_stage='Waiting for approval.', output='Current result')
                consent = agent._Consent(session, turn, card)
                consent({'progress': 'Earlier upload update'})
                self.assertEqual(card['rig_stage'], 'Waiting for approval.')
                self.assertEqual(card['output'], 'Current result')

    def test_expired_quote_retries_same_price_without_second_approval(self):
        from test_spending import PROJECT
        tool = descriptor()
        tool['approval'] = {'confirm_over_credits': 0}  # The real rig descriptor always asks.
        first={'quote_id':'qt_first','credits':20,'expires_at':'later'}
        second={**first,'quote_id':'qt_second'}
        with tempfile.TemporaryDirectory() as tmp:
            runtime=types.SimpleNamespace(lock=threading.Lock(),cache_dir=Path(tmp),quotes={},quote_requests={},
                                          handle_error=Mock(),manifest={})
            session,turn=state.new_session(),agent.Turn()
            card=state.item_tool(cloud.PREFIX+'auto_rig','Rig','');session['items'].append(card)
            consent=agent._Consent(session,turn,card)
            seen=[]
            replies=[cloud_http.CloudError('approve','quote_required',extra=first),
                     cloud_http.CloudError('expired','quote_expired',409),
                     cloud_http.CloudError('approve','quote_required',extra=second),{'status':'succeeded'}]
            def run(*args,**kwargs):
                reply=replies.pop(0)
                if isinstance(reply,Exception):raise reply
                self.assertEqual(seen, ['cost'])  # Initial quote asks; same-price renewal does not ask again.
                return reply
            module=types.SimpleNamespace(run=Mock(side_effect=run))
            def approve(_):
                seen.append(card['ask'])
                return True
            with account.approval_context(consent),patch.object(agent,'_redraw'),patch.object(agent,'_save_spend'), \
                    patch.object(agent,'_wait_for_approval',side_effect=approve):
                args=account.authorize(module,tool,{},{},project=PROJECT,cancel=threading.Event(),runtime=runtime,client=None)
                progress=Mock()
                account.submit(module,tool,args,{},project=PROJECT,on_progress=progress,
                               cancel=threading.Event(),runtime=runtime,client=None)
            self.assertIs(module.run.call_args_list[2].kwargs['on_progress'],progress)
            self.assertEqual(turn.paid_calls,1)

    def test_worker_rig_uses_one_visible_card_with_price_and_progress(self):
        parent,private=state.new_session(),state.new_session()
        parent_turn,worker=agent.Turn(),agent.Turn()
        worker.chat=(parent,parent_turn);worker.worker='Rig helper'
        card=state.item_tool(cloud.PREFIX+'auto_rig','Rig','')
        consent=agent._Consent(private,worker,card,asks=False)
        with patch.object(agent,'_redraw'),patch.object(agent,'_wait_for_card',return_value=True):
            consent({'progress':'Uploading your character.'})
            visible=parent['items'][0]
            consent({'text':'20 credits','credits':20,'threshold':0,'required':True})
            self.assertEqual(len(parent['items']),1)
            self.assertEqual(visible['status'],'running')
            self.assertIn('20 credits ($0.20)',visible['cost'])
            consent({'progress':{'message':'Rigging your character…','pct':30,'job_id':'job_worker'}})
            self.assertEqual(visible['cloud_job_id'],'job_worker')
            self.assertEqual(visible['rig_stage'],'Rigging your character…')
            consent.finish(types.SimpleNamespace(ok=True,text='Ready to pose.'))
            self.assertEqual(visible['status'],'done')
            self.assertEqual(visible['output'],'Ready to pose.')
            consent({'progress':{'message':'Delayed progress','pct':90,'job_id':'job_worker'}})
            self.assertEqual(visible['output'],'Ready to pose.')
