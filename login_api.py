"""Login to NH_SERVER and verify village state directly from the PC."""
import argparse
import base64
import getpass
import json
import math
import os
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = 'http://central.kageherostudio.com/game/lyto/login'


def decode_message(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        pass
    try:
        text = base64.b64decode(value, validate=True).decode('utf-8')
    except (ValueError, UnicodeError):
        return value
    try:
        return json.loads(text)
    except ValueError:
        return text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Redirect refused to avoid forwarding credentials.')


def authenticate(account, password):
    # Match runtime 2.5.7: GET, lowercase account/password, channel 108, lv 1.
    query = urllib.parse.urlencode(dict(accId=account.lower(), pwd=password.lower(), channel=108, lv=1))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(ENDPOINT + '?' + query, timeout=15) as response:
        return json.loads(response.read(65536))


def login_event(account, signature, server=1):
    # Exact runtime handshake, including string channel and empty device info.
    return {'type': 8, 'source': [account.lower(), str(server), 'LDGameRoom', '2.5.7', '99108', '', signature],
            'timeStamp': int(time.time() * 1000)}


def send_command(socket, name, value):
    socket.send(json.dumps({'type': 28, 'source': {name: value}, 'timeStamp': int(time.time() * 1000)},
                           separators=(',', ':')))


def receive_source(socket):
    try:
        raw = socket.recv()
    except Exception as error:
        if type(error).__name__ in ('WebSocketTimeoutException', 'timeout', 'TimeoutError'):
            return {}
        raise
    if not raw:
        raise RuntimeError('Server closed the connection.')
    event = json.loads(raw)
    if event.get('type') in (12, 25, 39):
        raise RuntimeError('Server ended the session.')
    source = event.get('source')
    return source if event.get('type') in (28, 29) and isinstance(source, dict) else {}


def wait_tower_state(socket, key):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        source = receive_source(socket)
        if isinstance(source.get(key), dict):
            return source[key]
    raise TimeoutError('Tower state was not received.')


DEFAULT_TEAM = (140, 207, 172)  # Himawari Uzumaki, Merz, Minato Kyuubi Mode, in slot order.


def ensure_team(socket, team, heroes):
    current = team.get('mars')
    if not isinstance(current, list):
        raise RuntimeError('Team state is missing; refusing tower battle.')
    if tuple(heroes.get(str(hid), {}).get('id') for hid in current) == DEFAULT_TEAM:
        print('Team verified: Himawari, Merz, Minato Kyuubi.', flush=True)
        return current
    desired = []
    for role in DEFAULT_TEAM:
        matches = [hid for hid, hero in heroes.items() if hero.get('id') == role]
        if len(matches) != 1 or not str(matches[0]).isdigit():
            raise RuntimeError('Default ninja missing or ambiguous; refusing tower battle.')
        desired.append(int(matches[0]))
    send_command(socket, 'setFighter', {'hids': desired})
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        source = receive_source(socket)
        updated = source.get('heros')
        if isinstance(updated, dict) and 'mars' in updated:
            if updated['mars'] != desired:
                raise RuntimeError('Team change not confirmed; refusing tower battle.')
            print('Team restored and verified: Himawari, Merz, Minato Kyuubi.', flush=True)
            return desired
    raise TimeoutError('Team change unconfirmed; do not retry blindly.')


def quit_tower(socket, snt=False):
    key, command, build = ('exam', 'endExam', 9) if snt else ('newexam', 'newEndExam', 16)
    send_command(socket, command, {'1': 1})
    state = wait_tower_state(socket, key)
    if state.get('currPass') != -1:
        # Quit can return a partial update; opening again is read-only, not another Quit.
        send_command(socket, 'openBuild', {'id': build})
        state = wait_tower_state(socket, key)
    if state.get('currPass') != -1:
        raise RuntimeError('Tower Quit not confirmed; do not retry blindly.')
    print('%s Quit confirmed; next run will require Enter.' % ('SNT' if snt else 'GST'), flush=True)
    return state


def run_tower(socket, floors=0, snt=False, quit_after=False):
    name, build, key, start, fight = ('SNT', 9, 'exam', 'doExamOption', 'examFight') if snt else (
        'GST', 16, 'newexam', 'doNewExamOption', 'newExamFight')
    send_command(socket, 'openBuild', {'id': build})
    state = wait_tower_state(socket, key)
    print(name, 'entered; completed floor:', state.get('currPass'), flush=True)
    completed = attempts = losses = 0
    while completed < floors:
        if state.get('currPass') == -1:
            costs = state.get('cost')
            cost = costs[0] if isinstance(costs, list) and costs else None
            # SNT idx 0 is the normal silver entry; never select idx 2 (gold).
            limit = 10000
            if type(cost) is not int or not 0 <= cost <= limit:
                raise RuntimeError('Refusing an unknown or excessive tower start cost.')
            if cost:
                print('%s normal entry: %s silver; no gold or extra attempts.' % (name, cost), flush=True)
            send_command(socket, start, {'idx': 0})
            state = wait_tower_state(socket, key)
        before = state.get('currPass')
        if not isinstance(before, int) or before < 0:
            raise RuntimeError('Tower has no active floor.')
        max_pass = state.get('maxPass')
        if isinstance(max_pass, int) and before >= max_pass:
            print('%s maximum floor %s reached; tower fully cleared.' % (name, max_pass), flush=True)
            if quit_after:
                print('Max floor reached; ending tower with Quit.', flush=True)
                return quit_tower(socket, snt)
            break
        if attempts:
            time.sleep(1)
        attempts += 1
        send_command(socket, fight, 1)
        deadline = time.monotonic() + 45
        result_received = lost = False
        updated = None
        while time.monotonic() < deadline:
            source = receive_source(socket)
            result_received |= 'fightRes' in source
            result = source.get('fightRes')
            if isinstance(result, dict) and result.get('win') in (0, False):
                losses += 1
                print('%s defeat %s/3 at floor %s.' % (name, losses, before + 1), flush=True)
                if losses >= 3:
                    if quit_after:
                        print('Third defeat; ending tower with Quit.', flush=True)
                        return quit_tower(socket, snt)
                    print('Quit: third defeat; disconnecting without resetting tower.', flush=True)
                    socket.close()
                    return state
                print('Retry same floor; no purchase or reset.', flush=True)
                lost = True
                break
            if isinstance(source.get(key), dict):
                updated = source[key]
            if result_received and updated is not None:
                break
        else:
            raise TimeoutError('Battle outcome was not confirmed; do not retry blindly.')
        if lost:
            continue
        state = updated
        after = state.get('currPass')
        if not isinstance(after, int) or after <= before:
            if quit_after:
                raise RuntimeError('Tower outcome ambiguous; refusing automatic Quit.')
            print(name, 'stopped: defeat/end or no confirmed floor advancement. No retry.', flush=True)
            break
        completed += 1
        print('%s victory confirmed: completed floor %s -> %s.' % (name, before, after), flush=True)
    if quit_after and floors and state.get('currPass', -1) >= 0:
        return quit_tower(socket, snt)
    return state


def war_state(socket):
    send_command(socket, 'requestWarInfo', 1)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        state = receive_source(socket).get('warInfo')
        if isinstance(state, dict) and {'allAttackers', 'currAttackers', 'currPass', 'status', 'num', 'total'} <= state.keys():
            return state
    raise TimeoutError('Complete Great Ninja War state was not received.')


def war_hp(state, hids):
    rates = state.get('allAttackers')
    if not isinstance(rates, dict):
        raise RuntimeError('Great Ninja War HP is missing.')
    hp = [rates.get(str(hid)) for hid in hids]
    if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in hp):
        raise RuntimeError('Great Ninja War HP is unknown; refusing battle.')
    return hp


def run_gnw(socket, hids, run=False):
    state = war_state(socket)
    print('GNW entered: stage %s, restart %s/%s.' % (state['currPass'], state['num'], state['total']), flush=True)
    if not run:
        return state
    if type(state['num']) is not int or type(state['total']) is not int:
        raise RuntimeError('Unknown Great Ninja War restart allowance.')
    if state['num'] == state['total'] == 1:
        send_command(socket, 'restartWar', 1)
        wait_tower_state(socket, 'warInfo')
        state = war_state(socket)
        if state['num'] != 0 or state['currPass'] != 0 or war_hp(state, hids) != [1, 1, 1]:
            raise RuntimeError('Great Ninja War restart not confirmed; no retry.')
        print('GNW free restart confirmed.', flush=True)
    elif state['num'] != 0:
        raise RuntimeError('Restart is not 1/1 or 0 remaining; refusing unknown reset.')
    if state['currAttackers'] != hids:
        send_command(socket, 'setWarAttackers', {'hids': hids})
        wait_tower_state(socket, 'warInfo')
        state = war_state(socket)
        if state['currAttackers'] != hids:
            raise RuntimeError('Great Ninja War team change not confirmed.')
    for attempt in range(1000):
        if state['currAttackers'] != hids:
            raise RuntimeError('Great Ninja War team changed; stopping.')
        hp = war_hp(state, hids)
        print('GNW stage %s; team HP: %s.' % (state['currPass'], ', '.join('%.1f%%' % (v * 100) for v in hp)), flush=True)
        if not any(hp):
            print('GNW quit: Himawari, Merz and Minato are all dead. No revive.', flush=True)
            return state
        before = state['currPass']
        if type(before) is not int or before < 0 or state['status'] not in (0, 1):
            raise RuntimeError('Unknown Great Ninja War stage/status.')
        # GNW requires the normal victory chest before the next battle, not tower cash-out.
        if before > 0 and state['status'] == 0:
            send_command(socket, 'getWarReward', 1)
            wait_tower_state(socket, 'warInfo')
            state = war_state(socket)
            if state['status'] != 1 or state['currPass'] != before:
                raise RuntimeError('Great Ninja War chest not confirmed.')
        if attempt:
            time.sleep(1)
        send_command(socket, 'doWarFight', {})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = receive_source(socket).get('fightRes')
            if isinstance(result, dict) and type(result.get('win')) is int and result['win'] in (0, 1):
                break
        else:
            raise TimeoutError('Great Ninja War battle unconfirmed; do not retry blindly.')
        state = war_state(socket)
        after_hp = war_hp(state, hids)
        print('GNW battle: %s; stage %s -> %s.' % ('victory' if result['win'] else 'defeat', before, state['currPass']), flush=True)
        if not any(after_hp):
            print('GNW quit: Himawari, Merz and Minato are all dead. No revive.', flush=True)
            return state
        if state['currPass'] == before and after_hp == hp:
            raise RuntimeError('Great Ninja War made no confirmed progress; stopping.')
    raise RuntimeError('Great Ninja War safety battle limit reached.')


def claim_ramen(socket, player):
    before = player.get('energy')
    if type(before) is not int or before < 0:
        raise RuntimeError('Unknown stamina value; refusing claim.')
    send_command(socket, 'giveEnergy', {})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if hasattr(socket, 'settimeout'):
                socket.settimeout(2.5)
            source = receive_source(socket)
        except Exception:
            break
        updated = source.get('player')
        if isinstance(updated, dict) and isinstance(updated.get('energy'), (int, float)) and updated['energy'] > before:
            print('Ramen stamina claimed: %s -> %s.' % (before, updated['energy']), flush=True)
            return
        result = source.get('operResult')
        if isinstance(result, dict) and result.get('type') in (9, '9'):
            if result.get('res') in (0, '0'):
                print('Ramen stamina claim confirmed by server.', flush=True)
                return
            raise RuntimeError('Ramen claim rejected/unconfirmed; no retry.')
    print('Ramen: stamina claim not available/already claimed; no purchase.', flush=True)


def beast_cooldown(buildings):
    cd = buildings.get('13', {}).get('cd')
    if not isinstance(cd, list) or len(cd) < 2 or type(cd[1]) is not int or not 0 <= cd[1] <= 86400:
        raise RuntimeError('Unknown Beast cooldown; refusing possible gold spend.')
    return cd[1]


def attack_beast(socket, player, buildings, attack=False):
    # Each invocation logs in afresh; type29 is initialization, not a refresh endpoint.
    active = player.get('beastOpen')
    if type(active) is not int or active < 0:
        raise RuntimeError('Unknown Tailed Beast availability.')
    if not active:
        print('BEAST_STATUS ' + json.dumps({'active': False, 'cooldown': 0, 'attacked': False}), flush=True)
        return
    cooldown = beast_cooldown(buildings)
    if not attack or cooldown:
        print('BEAST_STATUS ' + json.dumps({'active': True, 'cooldown': cooldown, 'attacked': False}), flush=True)
        return
    # beastFight ignores the cooldown UI: sending it early can spend 20 gold.
    # Require fresh server cd[1] == 0; never change heros.mars for this event.
    send_command(socket, 'beastFight', 1)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        source = receive_source(socket)
        if isinstance(source.get('player'), dict):
            player.update(source['player'])
        result = source.get('fightRes')
        if isinstance(result, dict) and type(result.get('win')) is int and result['win'] in (0, 1):
            break
    else:
        raise TimeoutError('Tailed Beast battle unconfirmed; do not retry blindly.')
    # Wait at least the normal five minutes, then log in and re-check server cooldown.
    # Never reuse the pre-battle cd=0 for a subsequent attack.
    cooldown = 300 if player['beastOpen'] else 0
    print('Tailed Beast normal attack confirmed; current team unchanged.', flush=True)
    print('BEAST_STATUS ' + json.dumps({'active': bool(player['beastOpen']),
                                       'cooldown': cooldown, 'attacked': True}), flush=True)


def enter_village(account, signature, stay=0, gst=False, gst_floors=0, snt=False, snt_floors=0, server=1, gnw=False, gnw_run=False, tower_quit=False, ramen=False, beast=False, beast_attack=False):
    import websocket
    socket = websocket.create_connection('ws://games%s.kageherostudio.com:%s/nadsocket' % (server, 6000 + server),
                                         timeout=20, suppress_origin=True, http_no_proxy=['*'])
    try:
        socket.send(json.dumps(login_event(account, signature, server), separators=(',', ':')))
        deadline = time.monotonic() + 45
        seen = set()
        team, heroes, player, buildings = {}, {}, {}, {}
        required = {'player', 'buildings', 'enterGame'}
        if gnw:
            required.update(('heros', 'hes'))
        logged_in = joined = started = False
        while time.monotonic() < deadline:
            raw = socket.recv()
            if not raw:
                raise RuntimeError('Server closed the game connection.')
            event = json.loads(raw)
            kind = event.get('type')
            if kind in (12, 25, 39):
                raise RuntimeError('Game server rejected or ended the session.')
            logged_in |= kind == 11
            joined |= kind == 24
            if kind == 26 and not started:
                started = True
                socket.send(json.dumps({'type': 29, 'cName': 'com.jelly.player.DefaultPlayerEvent',
                                        'timeStamp': int(time.time() * 1000)}, separators=(',', ':')))
            source = event.get('source')
            if kind in (28, 29) and isinstance(source, dict):
                seen.update(source)
                if isinstance(source.get('player'), dict):
                    player.update(source['player'])
                if isinstance(source.get('buildings'), dict):
                    buildings.update(source['buildings'].get('bd', {}))
                if isinstance(source.get('heros'), dict):
                    team.update(source['heros'])
                if isinstance(source.get('hes'), dict):
                    heroes.update(source['hes'])
            if logged_in and joined and started and required <= seen:
                print('SUCCESS: server %s session active; player, buildings and enterGame received.' % server, flush=True)
                break
        else:
            raise TimeoutError('Village initialization did not complete.')
        if gnw:
            hids = ensure_team(socket, team, heroes)
            run_gnw(socket, hids, gnw_run)
        elif gst or snt:
            run_tower(socket, snt_floors if snt else gst_floors, snt, tower_quit)
        if ramen:
            claim_ramen(socket, player)
        if beast:
            attack_beast(socket, player, buildings, beast_attack)
        if stay and socket.connected:
            print('Keeping session open for %s seconds; Ctrl+C to disconnect.' % stay, flush=True)
            until = time.monotonic() + stay
            while time.monotonic() < until:
                socket.settimeout(min(15, until - time.monotonic()))
                try:
                    raw = socket.recv()
                    if not raw or json.loads(raw).get('type') in (12, 25, 39):
                        raise RuntimeError('Game session ended.')
                except websocket.WebSocketTimeoutException:
                    socket.ping()
        return sorted(seen)
    finally:
        socket.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-http', action='store_true', help='Explicitly accept unencrypted credential transport used by this game.')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--account-only', action='store_true', help='Skip the game-server handshake.')
    tower = parser.add_mutually_exclusive_group()
    tower.add_argument('--gst', action='store_true', help='Enter God Shinobi Tower (NH_SERVER).')
    tower.add_argument('--snt', action='store_true', help='Enter Senior Ninja Trial (NH_SERVER).')
    tower.add_argument('--gnw', action='store_true', help='Inspect Great Ninja War (NH_SERVER).')
    tower.add_argument('--ramen', action='store_true', help='Claim available free ramen stamina.')
    tower.add_argument('--beast', action='store_true', help='Inspect Tailed Beast and cooldown; preserve current team.')
    parser.add_argument('--beast-attack', action='store_true', help='One normal Beast attack only if fresh cooldown is zero.')
    parser.add_argument('--tower-quit', action='store_true', help='End/cash-out GST or SNT after the run so next run requires Enter.')
    parser.add_argument('--gnw-run', action='store_true', help='Restart GNW only at 1/1, fight until the default team is dead; no revive.')
    parser.add_argument('--snt-floors', type=int, default=0, help='Advance up to this many SNT floors; quit after 3 total defeats, no purchases.')
    parser.add_argument('--gst-floors', type=int, default=0, help='Advance up to this many GST floors; quit after 3 total defeats, no purchases.')
    parser.add_argument('--stay', type=int, default=0, help='Keep village session open this many seconds after verification.')
    args = parser.parse_args()
    if args.self_test:
        assert decode_message('{"servers":[1],"sgin":"test"}')['servers'] == [1]
        assert decode_message('6LSm5Y+35LiN5a2Y5Zyo5oiW5a+G56CB6ZSZ6K+v') == '\u8d26\u53f7\u4e0d\u5b58\u5728\u6216\u5bc6\u7801\u9519\u8bef'
        assert decode_message('not base64') == 'not base64'
        assert decode_message(base64.b64encode(b'{"servers":[1]}').decode()) == {'servers': [1]}
        event = login_event('TEST@EXAMPLE.COM', 'dummy')
        assert event['type'] == 8
        assert event['source'] == ['test@example.com', '1', 'LDGameRoom', '2.5.7', '99108', '', 'dummy']
        assert all(isinstance(value, str) for value in event['source'])
        assert login_event('TEST@EXAMPLE.COM', 'dummy', 45)['source'][1] == '45'
        class FakeSocket:
            def __init__(self):
                self.sent = []
                self.connected = True
            def close(self):
                self.connected = False
            def send(self, value):
                self.sent.append(json.loads(value))
            def recv(self):
                return json.dumps({'type': 28, 'source': {'newexam': {'currPass': -1}}})
        fake = FakeSocket()
        assert run_tower(fake) == {'currPass': -1}
        assert fake.sent[0]['type'] == 28
        assert fake.sent[0]['source'] == {'openBuild': {'id': 16}}
        class BattleSocket(FakeSocket):
            def __init__(self, sources):
                super().__init__()
                self.sources = iter(sources)
            def recv(self):
                return json.dumps({'type': 28, 'source': next(self.sources)})
        heroes = {'16': {'id': 140}, '57': {'id': 207}, '110': {'id': 172}}
        correct = BattleSocket([])
        ensure_team(correct, {'mars': [16, 57, 110]}, heroes)
        assert not correct.sent
        changed = BattleSocket([{'heros': {'mars': [16, 57, 110]}}])
        ensure_team(changed, {'mars': [110, 57, 16]}, heroes)
        assert [e['source'] for e in changed.sent] == [{'setFighter': {'hids': [16, 57, 110]}}]
        for invalid in ({}, dict(heroes, duplicate={'id': 140})):
            blocked = BattleSocket([])
            try:
                ensure_team(blocked, {'mars': []}, invalid)
                raise AssertionError('Missing/ambiguous team must be refused')
            except RuntimeError:
                assert not blocked.sent
        rejected = BattleSocket([{'heros': {'mars': [110, 57, 16]}}])
        try:
            ensure_team(rejected, {'mars': []}, heroes)
            raise AssertionError('Unconfirmed team must be refused')
        except RuntimeError:
            assert len(rejected.sent) == 1
        battle = BattleSocket([{'newexam': {'currPass': -1, 'cost': [0]}},
                               {'newexam': {'currPass': 0}}, {'fightRes': {}},
                               {'newexam': {'currPass': 1}}])
        assert run_tower(battle, 1)['currPass'] == 1
        assert [e['source'] for e in battle.sent] == [
            {'openBuild': {'id': 16}}, {'doNewExamOption': {'idx': 0}}, {'newExamFight': 1}]
        defeat = BattleSocket([{'newexam': {'currPass': 1}}, {'fightRes': {}},
                               {'newexam': {'currPass': 1}}])
        assert run_tower(defeat, 5)['currPass'] == 1 and len(defeat.sent) == 2
        loss = BattleSocket([{'newexam': {'currPass': 40}}] + [{'fightRes': {'win': 0}}] * 3)
        assert run_tower(loss, 1)['currPass'] == 40 and len(loss.sent) == 4
        assert not loss.connected
        mixed = BattleSocket([{'newexam': {'currPass': 40}}, {'fightRes': {'win': 0}},
                              {'fightRes': {'win': 1}}, {'newexam': {'currPass': 41}},
                              {'fightRes': {'win': 0}}, {'fightRes': {'win': 0}}])
        assert run_tower(mixed, 10)['currPass'] == 41 and len(mixed.sent) == 5
        assert not mixed.connected
        paid = BattleSocket([{'newexam': {'currPass': -1, 'cost': [10001]}}])
        try:
            run_tower(paid, 1)
            raise AssertionError('Excessive start must be refused')
        except RuntimeError:
            assert len(paid.sent) == 1
        snt = BattleSocket([{'exam': {'currPass': -1, 'cost': [10000]}}, {'exam': {'currPass': 0}},
                            {'fightRes': {'win': 1}}, {'exam': {'currPass': 1}}] +
                           [{'fightRes': {'win': 0}}] * 3)
        assert run_tower(snt, 1000, snt=True)['currPass'] == 1 and not snt.connected
        assert [e['source'] for e in snt.sent] == [
            {'openBuild': {'id': 9}}, {'doExamOption': {'idx': 0}}] + [{'examFight': 1}] * 4
        expensive = BattleSocket([{'exam': {'currPass': -1, 'cost': [10001]}}])
        try:
            run_tower(expensive, 1, snt=True)
            raise AssertionError('Unexpected SNT entry cost must be refused')
        except RuntimeError:
            assert len(expensive.sent) == 1
        print('Self-test OK')
        return 0
    if args.stay < 0 or args.gst_floors < 0 or args.snt_floors < 0:
        parser.error('--stay and tower floor limits must be non-negative.')
    if args.gst_floors and not args.gst:
        parser.error('--gst-floors requires --gst.')
    if args.snt_floors and not args.snt:
        parser.error('--snt-floors requires --snt.')
    if args.gnw_run and not args.gnw:
        parser.error('--gnw-run requires --gnw.')
    if args.beast_attack and not args.beast:
        parser.error('--beast-attack requires --beast.')
    if args.tower_quit and not ((args.gst and args.gst_floors) or (args.snt and args.snt_floors)):
        parser.error('--tower-quit requires a GST/SNT battle run.')
    if args.account_only and (args.gst or args.snt or args.gnw or args.ramen or args.beast):
        parser.error('--account-only cannot be combined with a tower.')
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name('.env'), override=False)
    try:
        server = int(os.environ.get('NH_SERVER', '1'))
    except ValueError:
        parser.error('NH_SERVER must be an integer.')
    if not 1 <= server <= 51:
        parser.error('NH_SERVER must be between 1 and 51 (known server list).')
    if not args.allow_http:
        parser.error('Endpoint uses plain HTTP. Use --allow-http only if you accept this risk.')
    account = (os.environ.get('NH_EMAIL') or input('Account email: ')).strip()
    password = os.environ.get('NH_PASSWORD') or getpass.getpass('Password (not saved): ')
    if not account or not password:
        print('Account and password are required.')
        return 1
    try:
        result = authenticate(account, password)
    except Exception as error:
        # Do not print exception text: it may include the credential-bearing URL.
        print('Request failed:', type(error).__name__)
        return 1
    code = result.get('code')
    if code != 1:
        message = decode_message(result.get('msg', ''))
        if message == '\u8d26\u53f7\u4e0d\u5b58\u5728\u6216\u5bc6\u7801\u9519\u8bef':
            print('Rejected: account does not exist or password is incorrect.')
        else:
            print('Authentication rejected; application code:', code)
        return 1
    data = decode_message(result.get('msg', ''))
    if not isinstance(data, dict) or not data.get('sgin'):
        print('Response lacks expected login signature; game login is not verified.')
        return 1
    print('Account authenticated; login signature held in memory only.', flush=True)
    if args.account_only:
        return 0
    try:
        enter_village(account, data['sgin'], args.stay, args.gst, args.gst_floors, args.snt, args.snt_floors, server, args.gnw, args.gnw_run, args.tower_quit, args.ramen, args.beast, args.beast_attack)
    except KeyboardInterrupt:
        print('Disconnected.')
        return 0
    except Exception as error:
        print('Game session failed:', type(error).__name__)
        return 1
    print('Verification finished; PC session disconnected.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
