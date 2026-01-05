"""Automated Polymarket Synthetic Bond Trading Bot - Final Version."""
import os
import json
import time
import requests
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON

# Load credentials
load_dotenv()

PRIVATE_KEY = os.getenv('POLYMARKET_PRIVATE_KEY', '')
WALLET_ADDRESS = os.getenv('POLYMARKET_WALLET_ADDRESS', '')

# Remove 0x prefix if present
if PRIVATE_KEY.startswith('0x'):
    PRIVATE_KEY = PRIVATE_KEY[2:]

# Bot configuration
SCAN_INTERVAL_MINUTES = 30
MIN_VOLUME = 10000
MIN_DEPTH = 50
MAX_SPREAD = 0.05
MIN_YIELD = 0.01
MIN_HOURS = 12
MAX_HOURS = 48
POSITION_SIZE_PCT = 0.10
MAX_POSITION_SIZE = 10.0

POSITIONS_FILE = 'open_positions.json'


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_positions():
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_positions(positions):
    with open(POSITIONS_FILE, 'w') as f:
        json.dump(positions, f, indent=2)


def get_balance():
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider('https://polygon-rpc.com'))
        usdc = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
        abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]
        contract = w3.eth.contract(address=usdc, abi=abi)
        balance = contract.functions.balanceOf(Web3.to_checksum_address(WALLET_ADDRESS)).call()
        return balance / 1e6
    except Exception as e:
        log(f"Balance error: {e}")
        return 0


def fetch_markets():
    all_markets = []
    offset = 0
    
    while True:
        try:
            r = requests.get('https://gamma-api.polymarket.com/markets', 
                           params={'limit': 100, 'offset': offset, 'closed': False, 'active': True}, 
                           timeout=10)
            markets = r.json()
            if isinstance(markets, dict) and 'data' in markets:
                markets = markets['data']
            if not markets:
                break
            all_markets.extend(markets)
            if len(markets) < 100:
                break
            offset += 100
        except:
            break
    
    return all_markets


def filter_markets(markets):
    filtered = []
    now = datetime.now(timezone.utc)
    
    for m in markets:
        # Time filter
        end = m.get('endDate')
        if not end:
            continue
        
        try:
            end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
            hours = (end_dt - now).total_seconds() / 3600
            
            if not (MIN_HOURS <= hours <= MAX_HOURS):
                continue
            
            m['_hours'] = hours
        except:
            continue
        
        # Volume filter
        vol = m.get('volume24hr') or 0
        try:
            if float(vol) < MIN_VOLUME:
                continue
            m['_volume'] = float(vol)
        except:
            continue
        
        filtered.append(m)
    
    return filtered


def get_token_ids(market):
    result = {'yes': None, 'no': None}
    
    tokens_str = market.get('clobTokenIds')
    outcomes_str = market.get('outcomes')
    
    if not tokens_str or not outcomes_str:
        return result
    
    try:
        tokens = json.loads(tokens_str)
        outcomes = json.loads(outcomes_str)
        
        for i, outcome in enumerate(outcomes):
            if i < len(tokens):
                if outcome.lower() == 'yes':
                    result['yes'] = str(tokens[i])
                elif outcome.lower() == 'no':
                    result['no'] = str(tokens[i])
    except:
        pass
    
    return result


def analyze_ob(token_id, client):
    try:
        book = client.get_order_book(token_id)
        
        if not book or not hasattr(book, 'asks') or not hasattr(book, 'bids'):
            return None
        
        asks = [{'price': float(a.price), 'size': float(a.size)} for a in book.asks] if book.asks else []
        bids = [{'price': float(b.price), 'size': float(b.size)} for b in book.bids] if book.bids else []
        
        if not asks or not bids:
            return None
        
        best_ask = min(asks, key=lambda x: x['price'])
        best_bid = max(bids, key=lambda x: x['price'])
        
        return {
            'ask': best_ask['price'],
            'bid': best_bid['price'],
            'spread': best_ask['price'] - best_bid['price'],
            'depth': sum(b['size'] for b in bids if 0.90 <= b['price'] <= 0.98)
        }
    except:
        return None


def find_opps(markets, positions):
    client = ClobClient("https://clob.polymarket.com")
    opps = []
    
    log(f"Analyzing {len(markets)} markets...")
    
    for m in markets:
        tids = get_token_ids(m)
        
        if not tids['no'] or tids['no'] in positions:
            continue
        
        ob = analyze_ob(tids['no'], client)
        
        if not ob:
            continue
        
        if ob['spread'] > MAX_SPREAD or ob['depth'] < MIN_DEPTH:
            continue
        
        if 0.90 <= ob['ask'] <= 0.98:
            y = (1.0 - ob['ask']) / ob['ask']
            
            if y >= MIN_YIELD:
                m['_ob'] = ob
                m['_tids'] = tids
                m['_yield'] = y
                m['_price'] = ob['ask']
                opps.append(m)
    
    opps.sort(key=lambda x: x.get('_yield', 0), reverse=True)
    return opps


def place_trade(opp, balance, client):
    tid = opp['_tids']['no']
    price = opp['_price']
    
    stake = min(balance * POSITION_SIZE_PCT, MAX_POSITION_SIZE)
    size = stake / price
    
    if stake < 0.50:
        log(f"Insufficient balance: ${balance:.2f}")
        return None
    
    try:
        order = OrderArgs(token_id=tid, price=price, size=size, side='BUY', fee_rate_bps=0)
        signed = client.create_order(order)
        resp = client.post_order(signed, OrderType.GTC)
        
        oid = resp.get('orderID')
        log(f"✅ TRADE: {size:.0f} shares @ ${price:.3f} = ${stake:.2f}")
        log(f"   Order: {oid}")
        
        return oid
    except Exception as e:
        log(f"❌ Trade error: {e}")
        return None


def run_cycle():
    log("="*60)
    log("🤖 BOT CYCLE START")
    
    if not PRIVATE_KEY or not WALLET_ADDRESS:
        log("❌ Missing .env credentials")
        return
    
    # Initialize client with credentials
    log("🔐 Initializing trading client...")
    try:
        # Create client with private key
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=PRIVATE_KEY,
            chain_id=POLYGON
        )
        
        # Derive API credentials for trading
        log("   Deriving API credentials...")
        api_creds = client.create_or_derive_api_creds()
        log(f"   ✓ API Key: {api_creds.api_key[:10]}...")
        
        # Reinitialize with credentials
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=PRIVATE_KEY,
            chain_id=POLYGON,
            creds=api_creds
        )
        log("   ✓ Client ready for trading")
        
    except Exception as e:
        log(f"❌ Client setup error: {e}")
        import traceback
        traceback.print_exc()
        return
    
    balance = get_balance()
    log(f"💰 Balance: ${balance:.2f}")
    
    positions = load_positions()
    log(f"📊 Positions: {len(positions)}")
    
    log("🔍 Fetching markets...")
    all_markets = fetch_markets()
    log(f"   Total: {len(all_markets)}")
    
    filtered = filter_markets(all_markets)
    log(f"   Filtered: {len(filtered)}")
    
    opps = find_opps(filtered, positions)
    log(f"🎯 Opportunities: {len(opps)}")
    
    if opps:
        log("\n📋 TOP 5:")
        for i, o in enumerate(opps[:5], 1):
            log(f"   {i}. {o.get('question', 'Unknown')[:50]}")
            log(f"      ${o['_price']:.3f}, {o['_yield']:.2%}, ${o['_volume']:,.0f}")
    
    trades = 0
    
    for opp in opps[:3]:
        tid = opp['_tids']['no']
        
        if tid in positions:
            continue
        
        log(f"\n🚀 Trading: {opp.get('question', 'Unknown')[:50]}")
        
        oid = place_trade(opp, balance, client)
        
        if oid:
            positions[tid] = {
                'title': opp.get('question', 'Unknown'),
                'price': opp['_price'],
                'yield': opp['_yield'],
                'time': datetime.now().isoformat(),
                'order_id': oid
            }
            save_positions(positions)
            trades += 1
            balance = get_balance()
    
    log(f"\n✅ Cycle done. Trades: {trades}")
    log("="*60)


def main():
    log("🤖 Polymarket Bot Starting")
    log(f"⚙️  Scan: {SCAN_INTERVAL_MINUTES}min, Vol: ${MIN_VOLUME:,}, Yield: {MIN_YIELD:.1%}")
    
    while True:
        try:
            run_cycle()
        except Exception as e:
            log(f"❌ Cycle error: {e}")
            import traceback
            traceback.print_exc()
        
        log(f"\n⏰ Next scan in {SCAN_INTERVAL_MINUTES} min...")
        time.sleep(SCAN_INTERVAL_MINUTES * 60)


if __name__ == '__main__':
    main()
