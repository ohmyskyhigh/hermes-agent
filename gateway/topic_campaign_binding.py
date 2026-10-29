"""Campaign-specific trusted wake metadata and exact-message execution resolution."""
import json
from hermes_state_runtime import RuntimeStoreError


def campaign_metadata(metadata):
    if 'topic_campaign_key' not in metadata:
        return {}
    value = metadata['topic_campaign_key']
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise RuntimeStoreError('invalid_params')
    result = {'topic_campaign_key': value}
    if 'topic_campaign_dialogue' in metadata:
        origin = metadata['topic_campaign_dialogue']
        if not isinstance(origin, str) or not origin.strip() or len(origin) > 2048:
            raise RuntimeStoreError('invalid_params')
        result['topic_campaign_dialogue'] = origin
    if 'topic_campaign_epoch' in metadata:
        epoch = metadata['topic_campaign_epoch']
        if type(epoch) is not int or epoch < 0:
            raise RuntimeStoreError('invalid_params')
        result['topic_campaign_epoch'] = epoch
    return result


def restore_campaign_metadata(event, descriptor):
    event.metadata.update(campaign_metadata(descriptor))
    event._campaign_wake_trusted = True
    origin = descriptor.get('topic_campaign_dialogue')
    if origin:
        try:
            identity = json.loads(descriptor.get('identity', ''))
        except (TypeError, ValueError):
            identity = None
        event._campaign_dialogue_trusted = (isinstance(identity, list) and len(identity) == 3
                                           and identity[0] == 'campaign-dialogue' and identity[2] == origin)



async def resolve_campaign(service, event, source):
    from gateway.topic_campaign_config import topic_key
    from gateway.session_identity import identity_of
    topic = topic_key(source)
    if not event.internal:
        binding = await service.db('binding', topic, event.message_id or source.message_id or '')
        if binding is None or binding['disposition'] != 'bound':
            return None, binding is not None
        return await service.db('get', binding['campaign_key']), True
    key = campaign_metadata(event.metadata).get('topic_campaign_key')
    if key:
        if not getattr(event, '_campaign_wake_trusted', False):
            return None, True
        item = await service.db('get', key)
        identity = identity_of(source)
        profile = identity.runtime_profile if identity else source.profile or service.runner._active_profile_name()
        origin = event.metadata.get('topic_campaign_dialogue')
        intent = await service.db('input_intent', key, origin) if origin else None
        targets = intent['targets'] if intent and intent['mode'] == 'paused_dialogue' else [item['owner_profile']] if item else []
        if item is None or item['topic_key'] != topic or profile not in targets or (origin and not intent):
            return None, True
        return item, True
    # Legacy unbound completion notices retain their old path except while this topic is paused.
    current = await service.db('current', topic)
    if current and current['status'] == 'paused':
        return None, True
    return None, False
