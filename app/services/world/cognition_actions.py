from __future__ import annotations

from app.services.world import actions
from app.services.world.models import ActionRequest
from app.storage.world_store import WorldError, entity


def build_menu(c, w, context, catalog):
    aid, wid = context['actor_id'], w['world_id']
    actor = entity(c, wid, aid)
    known = {x['fact']['entity_id']: x['fact'] for x in context['observations']}
    for place in context['destinations']:
        known.setdefault(place['entity_id'], place)
    visible = {x['entity_id'] for x in actions.visible(c, wid, actor)}
    destinations = {x['entity_id'] for x in context['destinations']}
    candidates = []

    def add(operation, target, **params):
        candidate = {'operation': operation, 'target_id': target, 'params': params}
        if candidate not in candidates:
            candidates.append(candidate)

    values = list(known.values())
    for value in values:
        key, state, caps = value['entity_id'], value.get('state', {}), value.get('capabilities', [])
        if key in destinations:
            add('move', key)
        if key in visible and value['kind'] != 'actor':
            add('inspect', key)
        if 'portable' in caps:
            if value.get('parent_id') != aid:
                add('take', key)
            # A future put may follow a take; it is not offered as a valid first step until held.
            places = [x for x in values if x['entity_id'] == actor['parent_id'] or 'container' in x.get('capabilities', [])]
            for dest in places[:5]:
                if dest['entity_id'] != key:
                    add('put', key, destination_id=dest['entity_id'])
            if state.get('price_credits'):
                add('buy', key, quantity=1)
            if state.get('quantity', 1) > 1:
                add('split', key, quantity=1)
        if 'openable' in caps:
            add('close' if state.get('open') else 'open', key)
        if 'toggleable' in caps:
            add('turn_off' if state.get('on') else 'turn_on', key)
        if 'readable' in caps:
            add('read', key)
        if 'washable' in caps and state.get('dirty'):
            add('clean', key)
        if 'edible' in caps and state.get('quantity', 1) > 0:
            add('consume', key, quantity=1)
        if 'repairable' in caps and state.get('broken'):
            tools = [x for x in values if 'repair_tool' in x.get('capabilities', [])]
            parts = [x for x in values if 'repair_part' in x.get('capabilities', []) and x.get('state', {}).get('part_kind') == state.get('required_part')]
            for tool in tools[:2]:
                for part in parts[:2]:
                    add('repair', key, tool_id=tool['entity_id'], part_id=part['entity_id'])
        if 'cookware' in caps:
            ingredients = [x['entity_id'] for x in values if 'edible' in x.get('capabilities', []) and x.get('state', {}).get('quantity', 1) > 0]
            if ingredients:
                add('cook', key, ingredients=ingredients[:3])
        if 'container' in caps and state.get('volume_ml', 0) > 0:
            for dest in values:
                ds = dest.get('state', {})
                free = ds.get('capacity_ml', 0) - ds.get('volume_ml', 0)
                if dest['entity_id'] != key and 'container' in dest.get('capabilities', []) and free > 0:
                    add('pour', key, destination_id=dest['entity_id'], volume_ml=min(200, free, state['volume_ml']))
    # Prefer useful actions over inspection/placement combinations when a room is crowded.
    candidates.sort(key=lambda a: (a['operation'] in {'inspect', 'put'}, a['target_id'] not in visible))
    menu = []
    for candidate in candidates[:64]:
        refs = [candidate['target_id']] + [v for k, v in candidate['params'].items() if k.endswith('_id')] + candidate['params'].get('ingredients', [])
        allowed_refs = visible | destinations if candidate['operation'] == 'move' else visible
        valid, reason = False, 'Not currently observed; requires movement or fresh observation'
        if all(ref in allowed_refs for ref in refs):
            request = ActionRequest(request_id='affordance', actor_id=aid, **candidate).model_dump(mode='json')
            c.execute('SAVEPOINT affordance_probe')
            try:
                actions.execute(c, w, 'affordance-probe', request, catalog)
                valid, reason = True, None
            except WorldError as error:
                reason = str(error)[:140]
            finally:
                c.execute('ROLLBACK TO affordance_probe')
                c.execute('RELEASE affordance_probe')
        menu.append({'key': f'a{len(menu)}', **candidate, 'available_now': valid, 'precondition': reason})
    return menu


def actor_schema(context):
    def obj(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}

    def nullable_enum(values):
        return {'type': ['string', 'null'], 'enum': [*values, None]} if values else {'type': 'null'}

    fact_ids = [x['fact']['entity_id'] for x in context['observations']]
    condition = obj({'entity_id': {'type': 'string', 'enum': fact_ids}, 'field': {'type': 'string'},
                     'equals': {'type': ['string', 'number', 'boolean', 'null']}})
    goal = obj({'title': {'type': 'string'}, 'motivation': {'type': 'string'},
                'conditions': {'type': 'array', 'items': condition, 'minItems': 1, 'maxItems': 5}})
    menu = context['action_menu']
    schema = obj({'summary': {'type': 'string'}, 'plan': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 6},
        'action_key': nullable_enum([a['key'] for a in menu if a['available_now']]),
        'follow_up_keys': {'type': 'array', 'items': {'type': 'string', 'enum': [a['key'] for a in menu]}, 'maxItems': 4},
        'new_goal': {'anyOf': [goal, {'type': 'null'}]},
        'thread_id': nullable_enum(context['allowed_thread_ids']),
        'abandon_thread_id': nullable_enum([t['thread_id'] for t in context['threads']]),
        'wait_seconds': {'type': 'integer', 'minimum': 60, 'maximum': 86400}})
    if not menu:
        schema['properties']['follow_up_keys'] = {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 0}
    if not fact_ids:
        schema['properties']['new_goal'] = {'type': 'null'}
    return schema


def strict_schema(schema):
    if isinstance(schema, dict):
        result = {k: ({name: strict_schema(spec) for name, spec in v.items()}
                      if k in {'properties', '$defs'} else strict_schema(v))
                  for k, v in schema.items() if k not in {'default', 'title'}}
        if result.get('type') == 'object':
            result['additionalProperties'] = False
            result['required'] = list(result.get('properties', {}))
        return result
    if isinstance(schema, list):
        return [strict_schema(x) for x in schema]
    return schema


def resolve_choices(value, context):
    if not isinstance(value, dict) or 'action_key' not in value:
        return value
    data = dict(value)
    menu = {x['key']: x for x in context['action_menu']}

    def resolve(key, first=False):
        if not isinstance(key, str) or key not in menu:
            raise WorldError('Unknown action choice')
        selected = menu[key]
        if first and not selected['available_now']:
            raise WorldError('First action does not meet observed preconditions')
        return {k: selected[k] for k in ('operation', 'target_id', 'params')}

    if 'action' in data or 'follow_up_actions' in data:
        raise WorldError('Do not mix action keys and free-form actions')
    first = data.pop('action_key')
    data['action'] = resolve(first, first=True) if first is not None else None
    follows = data.pop('follow_up_keys', [])
    if not isinstance(follows, list) or len(follows) > 4:
        raise WorldError('Invalid follow-up action choices')
    data['follow_up_actions'] = [resolve(key) for key in follows]
    return data
