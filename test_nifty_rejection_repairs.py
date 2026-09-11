import unittest
from copy import deepcopy
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

import vamsi_nifty_option_buy as strategy
from test_vamsi_nifty_option_buy import candidate


NOW = datetime(2026, 9, 8, 10, 0, 10, tzinfo=ZoneInfo('Asia/Kolkata'))


def row(role='NEXT_EXPIRY_ATM', delta=.50, spread=1.0, direction='BULLISH'):
    prefix = 'CE' if direction == 'BULLISH' else 'PE'
    tier = {'NEXT_EXPIRY_ATM': 0, 'NEXT_EXPIRY_ITM': 1, 'SAME_EXPIRY_ITM': 2}[role]
    return {
        'strike': 25000 if tier == 0 else 24950 if prefix == 'CE' else 25050,
        'expiry': '2026-09-08' if tier == 2 else '2026-09-15',
        'timestamp': NOW.isoformat(),
        '_contract_selection_role': role, '_contract_selection_tier': tier,
        f'{prefix}_ltp': 150, f'{prefix}_bid_price': 150-spread/2,
        f'{prefix}_ask_price': 150+spread/2, f'{prefix}_delta': delta,
        f'{prefix}_iv': 15, f'{prefix}_bid_qty': 650, f'{prefix}_ask_qty': 650,
    }


class RejectionRepairTests(unittest.TestCase):
    def choose(self, rows, base=None, refresh=None):
        instrument = {'instrument_key': 'NSE_FO|test', 'trading_symbol': 'NIFTY TEST', 'lot_size': 65}
        with (
            patch.object(strategy.trade_bot, 'index_contract_rows', return_value=rows),
            patch.object(strategy.trade_bot, 'find_index_option_instrument', return_value=instrument),
            patch.object(strategy.trade_bot, 'now_ist', return_value=NOW),
            patch.object(strategy.trade_bot, 'fetch_upstox_option_chain', return_value=refresh) as fetch,
        ):
            result = strategy.select_complete_contract(base or candidate(), {}, NOW)
        return (*result, fetch.call_count)

    def test_valid_itm_is_used_after_atm_spread_rejection(self):
        selected, decision, _ = self.choose([row(spread=6), row('NEXT_EXPIRY_ITM', delta=.67)])
        self.assertTrue(decision['allowed'])
        self.assertEqual(selected['option_summary']['contract_selection_role'], 'NEXT_EXPIRY_ITM')
        self.assertEqual(len(decision['contract_attempts']), 2)
        self.assertGreater(selected['entry_price'], 150)

    def test_valid_atm_keeps_priority(self):
        selected, decision, _ = self.choose([row(), row('NEXT_EXPIRY_ITM', delta=.67)])
        self.assertTrue(decision['allowed'])
        self.assertEqual(selected['option_summary']['contract_selection_role'], 'NEXT_EXPIRY_ATM')
        self.assertEqual(len(decision['contract_attempts']), 1)

    def test_put_uses_absolute_delta_but_validates_negative_sign(self):
        selected, decision, _ = self.choose(
            [row(delta=-.5, direction='BEARISH')], candidate('BEARISH', target=80)
        )
        self.assertTrue(decision['allowed'])

    def test_missing_greeks_refresh_and_recover_same_contract(self):
        refreshed = pd.DataFrame([row()])
        _, decision, calls = self.choose([row(delta=0)], refresh=(refreshed, refreshed, refreshed))
        self.assertTrue(decision['allowed'])
        self.assertEqual(calls, 1)

    def test_missing_greeks_do_not_fall_into_expiry_day_contract(self):
        bad = row(delta=0)
        refreshed = pd.DataFrame([bad])
        _, decision, calls = self.choose(
            [bad, row('SAME_EXPIRY_ITM', delta=.7)], refresh=(refreshed, refreshed, refreshed)
        )
        self.assertFalse(decision['allowed'])
        self.assertEqual(calls, 1)
        self.assertEqual(len(decision['contract_attempts']), 1)
        self.assertIn('Greeks', str(decision['blockers']))

    def test_weak_structure_is_not_rescued_by_another_contract(self):
        base = candidate()
        base['technicals']['entry_structure']['qualified'] = False
        _, decision, calls = self.choose([row(), row('NEXT_EXPIRY_ITM', .65)], base)
        self.assertFalse(decision['allowed'])
        self.assertNotIn('contract_attempts', decision)
        self.assertEqual(calls, 0)

    def test_early_expiry_fallback_requires_stronger_score(self):
        base = candidate()
        base['technicals']['five_min']['volume_ratio'] = 0
        base['technicals']['five_min']['vwap_bias'] = 'NEUTRAL'
        base['technicals']['nifty_breadth']['bias'] = 'BEARISH'
        # 65 points passes the ordinary 60 threshold but fails expiry-day 70.
        _, decision, _ = self.choose([row('SAME_EXPIRY_ITM', .7)], base)
        self.assertFalse(decision['allowed'])
        self.assertIn('expiry-day alignment', str(decision['blockers']))

    def test_same_expiry_final_gate_checks_cutoff_again(self):
        base = candidate()
        base['option_summary']['option_market_quality'].update(
            contract_role='SAME_EXPIRY_ITM', delta=.7, bid_qty=650, ask_qty=650
        )
        decision = strategy.evaluate_candidate(base, NOW.replace(hour=12))
        self.assertFalse(decision['allowed'])
        self.assertIn('entry window', str(decision['blockers']))

    def test_chain_disagreement_does_not_choose_wrong_side(self):
        t = candidate()['technicals']
        with patch.object(strategy.trade_bot, 'classify_market_regime', return_value=t['market_regime']):
            value = strategy.market_candidate({'direction': 'BEARISH', 'confidence': 'HIGH'}, t, NOW)
        self.assertEqual(value['direction'], 'BULLISH')
        self.assertEqual(value['option_summary']['chain_bias'], 'BEARISH')
        self.assertEqual(value['option_summary']['option_type'], 'CE')
        self.assertEqual(strategy.weighted_signal(value)['components']['option_chain']['earned'], 0)

    def test_real_nearby_resistance_is_not_skipped_to_force_rr(self):
        value = candidate()
        value['technicals']['five_min']['recent_swing_high'] = 100.2
        decision = strategy.underlying_trade_plan(value)
        self.assertFalse(decision['allowed'])
        self.assertAlmostEqual(decision['target_points'], .2)
        self.assertIn('5M swing high', decision['reason'])

    def test_fabricated_percentage_target_is_excluded(self):
        value = candidate()
        for name in ('five_min', 'fifteen_min'):
            value['technicals'][name].update(
                recent_swing_high=99, upper_band=99, target=120, target_is_fallback=True
            )
        self.assertFalse(strategy.underlying_trade_plan(value)['allowed'])

    def test_disabled_reentry_reset_ignores_old_same_day_guard(self):
        with (
            patch.object(strategy.trade_bot, 'read_reentry_guard', return_value={
                'mode': 'signal_reset', 'blocked_direction': 'BULLISH', 'reset_seen': False
            }),
            patch.object(strategy.trade_bot, 'require_signal_reset_for_same_index_reentry', return_value=False),
        ):
            self.assertEqual(strategy.trade_bot.reentry_block_reason('NIFTY', 'BULLISH'), '')

    def test_stale_or_incomplete_candles_are_explicit(self):
        self.assertEqual(strategy.candle_data_problem({'candle_time': NOW.replace(minute=55, hour=9).isoformat(), 'close': 100}, 5, NOW), '')
        self.assertTrue(strategy.candle_data_problem({'candle_time': NOW.isoformat(), 'close': 100}, 5, NOW))
        self.assertTrue(strategy.candle_data_problem({'candle_time': '2026-09-07T15:15:00+05:30', 'close': 100}, 5, NOW))

    def test_collection_uses_underlying_direction_and_prepares_executable_itm(self):
        t = candidate()['technicals']
        t['five_min']['candle_time'] = NOW.replace(hour=9, minute=55).isoformat()
        t['fifteen_min'].update(candle_time=NOW.replace(hour=9, minute=45).isoformat(), close=100)
        atm = row(spread=6)
        itm = row('NEXT_EXPIRY_ITM', .67)
        rec = {
            'symbol': 'NIFTY', 'direction': 'BEARISH', 'confidence': 'HIGH',
            'atm': atm, 'nearby_contracts': [atm, itm],
            'analysis_expiry': '2026-09-08', 'execution_expiry': '2026-09-15',
        }
        with (
            patch.object(strategy.trade_bot, 'trading_engine', return_value=strategy.ENGINE),
            patch.object(strategy.trade_bot, 'get_index_recommendation', return_value=rec),
            patch.object(strategy.trade_bot, 'record_option_chain_snapshot'),
            patch.object(strategy.trade_bot, 'ensure_instruments_file'),
            patch.object(strategy.trade_bot, 'get_technical_analysis', return_value=t),
            patch.object(strategy, 'add_futures_participation'),
            patch.object(strategy.trade_bot, 'get_nifty_breadth', return_value={'bias': 'BULLISH'}),
            patch.object(strategy.trade_bot, 'classify_market_regime', return_value=t['market_regime']),
            patch.object(strategy.trade_bot, 'now_ist', return_value=NOW),
            patch.object(strategy.trade_bot, 'find_index_option_instrument', return_value={
                'instrument_key': 'NSE_FO|TEST', 'trading_symbol': 'NIFTY CE', 'lot_size': 65
            }),
        ):
            selected, decision = strategy.collect_candidate(NOW)
            prepared = strategy.prepare_candidate(selected, decision)
        self.assertTrue(decision['allowed'])
        self.assertEqual(prepared['direction'], 'BULLISH')
        self.assertEqual(prepared['transaction_type'], 'BUY')
        self.assertEqual(prepared['capital_override'], 'MAX')
        self.assertEqual(decision['option_quality']['contract_role'], 'NEXT_EXPIRY_ITM')
        self.assertEqual(prepared['option_delta_used'], .67)
        self.assertLess(prepared['stop_loss_price'], prepared['entry_price'])
        self.assertGreater(prepared['target_price'], prepared['entry_price'])

    def test_futures_vwap_is_translated_to_spot_price_units(self):
        t = candidate()['technicals']
        stamp = NOW.replace(hour=9, minute=55).isoformat()
        t['five_min']['candle_time'] = stamp
        futures = {
            'candle_time': stamp, 'close': 120, 'vwap': 117, 'vwap_bias': 'BULLISH',
            'volume': 1600, 'volume_ma20': 1000, 'volume_ratio': 1.6,
        }
        with (
            patch.object(strategy, 'nearest_index_future', return_value={'instrument_key': 'FUT', 'trading_symbol': 'NIFTY FUT'}),
            patch.object(strategy.market_technicals, 'fetch_v3_historical_minutes'),
            patch.object(strategy.market_technicals, 'fetch_v3_intraday_minutes'),
            patch.object(strategy.market_technicals, 'merge_candles'),
            patch.object(strategy.market_technicals, 'completed_candles'),
            patch.object(strategy.market_technicals, 'analyze_latest', return_value=futures),
        ):
            strategy.add_futures_participation(t, NOW)
            self.assertEqual(t['five_min']['vwap'], 97)
            self.assertEqual(t['five_min']['close'], 100)
            self.assertEqual(t['participation']['futures_vwap'], 117)
            futures['volume'] = 0
            with self.assertRaisesRegex(ValueError, 'volume'):
                strategy.add_futures_participation(deepcopy(t), NOW)


if __name__ == '__main__':
    unittest.main()
