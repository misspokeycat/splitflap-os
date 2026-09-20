"""Autocomplete backends for the settings UI."""

from datetime import datetime
import logging
import pytz
import requests
from flask import Blueprint, jsonify, request

bp = Blueprint("search", __name__)


@bp.route('/location_search')
def location_search_route():
    """Search for locations globally via Nominatim."""
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify(results=[])
    try:
        url = f'https://nominatim.openstreetmap.org/search?q={query}&format=json&limit=6&addressdetails=1'
        data = requests.get(url, timeout=5, headers={'User-Agent': 'SplitFlapOS/1.0'}).json()
        results = []
        for r in data:
            name = r.get('display_name', query)
            addr = r.get('address', {})
            short_name = (addr.get('city') or addr.get('town') or addr.get('village')
                          or addr.get('municipality') or name.split(',')[0].strip())
            results.append({
                'lat': r['lat'],
                'lon': r['lon'],
                'name': name,
                'short_name': short_name,
                'value': f"{r['lat']},{r['lon']}|{query}",
                'label': name,
            })
        return jsonify(results=results)
    except Exception as e:
        logging.error(f"Location search error: {e}")
        return jsonify(results=[], error=str(e)), 502

@bp.route('/location_timezone')
def location_timezone_route():
    """Get timezone for a lat/lon via Open-Meteo."""
    lat = request.args.get('lat', '')
    lon = request.args.get('lon', '')
    if not lat or not lon:
        return jsonify(timezone='')
    try:
        data = requests.get(
            'https://api.open-meteo.com/v1/forecast',
            params={'latitude': lat, 'longitude': lon, 'forecast_days': 1, 'current': 'temperature_2m'},
            timeout=5
        ).json()
        return jsonify(timezone=data.get('timezone', ''))
    except Exception:
        return jsonify(timezone='')

@bp.route('/timezones')
def timezones_route():
    """Search timezones with common ones first."""
    query = request.args.get('q', '').strip().lower()
    common = ['US/Eastern','US/Central','US/Mountain','US/Pacific','US/Hawaii',
              'Europe/London','Europe/Paris','Europe/Berlin','Asia/Tokyo','Asia/Shanghai',
              'Australia/Sydney','Pacific/Auckland','America/Chicago','America/Denver',
              'America/Los_Angeles','America/New_York','America/Toronto','America/Sao_Paulo']
    all_zones = pytz.common_timezones
    results = []
    seen = set()
    def add_zone(tz):
        if tz in seen: return
        seen.add(tz)
        try:
            offset = datetime.now(pytz.timezone(tz)).strftime('%z')
            label = f"{tz} (UTC{offset[:3]}:{offset[3:]})"
        except Exception:
            label = tz
        results.append({'value': tz, 'label': label})
    if not query:
        for tz in common: add_zone(tz)
    else:
        for tz in common:
            if query in tz.lower(): add_zone(tz)
        for tz in all_zones:
            if query in tz.lower(): add_zone(tz)
    return jsonify(zones=results[:20])

@bp.route('/stocks_search')
def stocks_search_route():
    """Search stock tickers via Yahoo Finance autocomplete."""
    query = request.args.get('q', '').strip()
    if len(query) < 1:
        return jsonify(tickers=[])
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=8&newsCount=0&enableFuzzyQuery=false&quotesQueryId=tss_match_phrase_query"
        data = requests.get(url, timeout=5, headers={'User-Agent':'Mozilla/5.0'}).json()
        tickers = []
        for q in data.get('quotes', []):
            sym = q.get('symbol', '')
            name = q.get('shortname') or q.get('longname') or ''
            if sym:
                tickers.append({'value': sym, 'label': f"{sym} — {name}" if name else sym})
        return jsonify(tickers=tickers)
    except Exception as e:
        logging.error(f"Stock search error: {e}")
        return jsonify(tickers=[], error=str(e)), 502

