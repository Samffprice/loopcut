"""Short auto-rig copy shared by chat, Jobs, and tool results."""

# What the 20 credits buy, in one sentence (ar-artist-1 asked about fingers and weights at the card). Fingers
# only when found: 19- and 22-bone standard rigs have none. The platform's quote still carries its own line.
QUOTE = 'Body bones with automatic weights, and finger bones when it can find them; best on humanoids, others may bend oddly.'
TIME_HINT = 'usually under a minute'
# The credits arm's refusal (0.2.19, cloud.error_text): the panel's out-of-credits pop-up has the way on, so no
# "Add credits, then retry" (the free plan cannot buy credits) and no retry.
CREDITS_OFFERED = 'Not enough credits for this rig; the panel shows how to get more.'
STAGES = {
    'checking': 'Checking character',
    'uploading': 'Preparing character',
    'quoting': 'Getting price',
    'queued': 'Waiting to rig',
    'running': 'Rigging character',
    'applying': 'Adding rig',
    'validating': 'Checking movement',
    'validating_experimental': 'Checking rig',
    'refund': 'Requesting refund',
}


def progress(stage):
    return f'{stage} — {TIME_HINT}.'


def price(credits, balance=None, *, settled=False):
    from .cloud_account import dollars
    if credits is None:
        return 'Charge unconfirmed'
    text = (f'{"Used " if settled else ""}{credits:g} credits ({dollars(credits)})'
            if credits else 'No credits used')
    if balance is not None:
        text += f' · Balance after: {max(0, balance - credits):,g}'
    return text


def approval(kind, notice):
    return {
        'age_confirmation': "Confirm you're 18 or older on your account, then retry.",
        'budget': 'Chat budget reached; extend it to continue.',
        'burst': 'Paid-call limit reached; allow more to continue.',
        'daily_limit_reached': 'Daily limit reached; approve this rig or change your limit.',
        'rate_limited': 'Paid calls paused; approve this rig to continue.',
    }.get(kind, notice or QUOTE)


def error(error):
    """Typed recovery copy; raw service diagnostics never enter a rig card or model result."""
    code = getattr(error, 'code', '')
    extra = getattr(error, 'extra', {})
    job = extra.get('job') if isinstance(extra.get('job'), dict) else {}
    credits = job.get('credits') if isinstance(job.get('credits'), dict) else {}
    if code == 'vendor_failed' and extra.get('rig_code') == 'unsupported_anatomy':
        reason = {
            'finger_count': 'finger layout', 'front_ambiguous': 'facing direction',
            'pelvis_branches': 'hip and leg layout', 'too_few_joints': 'body structure',
            'not_humanoid': 'body layout',
        }.get(extra.get('anatomy_reason') if isinstance(extra.get('anatomy_reason'), str) else '', 'body layout')
        refund = extra.get('refunded')
        confirmed = (refund is True or type(refund) in (int, float) and 0 < refund < float('inf'))
        no_charge = (confirmed and credits.get('charged', 0) == 0 or
                     job.get('status') == 'failed' and credits.get('charged') == 0 and credits.get('reserved') == 0)
        settlement = 'no credits used' if no_charge else 'charge unconfirmed'
        return f'Could not rig this {reason}; {settlement}, so adjust the character and retry.'
    if code == 'vendor_failed':
        refunded = credits.get('refunded')
        if type(refunded) in (int, float) and 0 < refunded < float('inf') and credits.get('charged') == 0:
            return f'Rigging failed; {refunded:g} credits returned, so retry.'
        return 'Rigging failed; check this job again before retrying.'
    return {
        'age_confirmation_required': approval('age_confirmation', ''),
        'triangle_limit': 'Over 100,000 triangles; make a lower-detail copy and rig that copy.',
        'vertex_limit': 'Over 300,000 points; make a lower-detail copy and rig that copy.',
        'invalid_mesh': 'Repair the character mesh, then retry.',
        'invalid_input': 'Select the character meshes and retry with the default rig settings.',
        'invalid_mode': 'Switch to Object Mode, then retry.',
        'shared_mesh': 'Make independent, editable copies of the character parts, then retry.',
        'already_rigged': 'Use the existing rig, or remove it before rigging again.',
        'group_conflict': 'Rename the conflicting bone groups, or use the existing rig.',
        'invalid_transform': 'Restore a positive scale on the character and scene, then retry.',
        'unsupported_mode': 'Use the default rig settings; facial expressions and hand or foot controls are unavailable.',
        'too_many_jobs': 'Finish or cancel a pending rig before starting another.',
        'ambiguous_selection': 'Select just one character, including its clothes, hair and eyes.',
        'source_changed': 'The character changed; select it again and retry.',
        'declined': 'Rigging skipped; approve a new call when ready.',
        'approval_required': 'Approve this rig to continue.',
        'canceled': 'Rigging canceled; retry when ready.',
        'connection_failed': 'Connection lost; retry to check this rig.',
        'job_unconfirmed': 'Unconfirmed; this rig may still run, so check it before starting another.',
        'quote_changed': 'The price changed; approve a new quote to continue.',
        'insufficient_credits': 'Add credits, then retry.',
        'daily_limit_reached': approval('daily_limit_reached', ''),
        'rate_limited': 'Too many requests; wait a moment and retry.',
        'concurrency_limit': 'Finish a pending job, then retry.',
        'private_mode': 'Your privacy settings block this rig; review them in Settings.',
        'generation_disabled': 'Generation is disabled; review your account settings.',
        'tool_disabled': 'Rigging is unavailable; refresh the tools and retry.',
        'contract_unsupported': 'Update Loopcut to use rigging.',
        'unauthorized': 'Sign in to Loopcut, then retry.',
        'auth_required': 'Sign in to Loopcut, then retry.',
        'validation_failed': 'The rig failed its movement check; your character is unchanged.',
    }.get(code, 'Could not add the rig; your character is unchanged, so retry or contact support.')
