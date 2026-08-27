"""ESPN league registry.

Keys are the suffixes of the ``sports_*`` settings; paths are the ESPN
scoreboard API segments.
"""

SPORTS_LEAGUES = {
    'nfl':    {'path': 'football/nfl',                       'name': 'NFL'},
    'nba':    {'path': 'basketball/nba',                     'name': 'NBA'},
    'mlb':    {'path': 'baseball/mlb',                       'name': 'MLB'},
    'nhl':    {'path': 'hockey/nhl',                         'name': 'NHL'},
    'ncaaf':  {'path': 'football/college-football',          'name': 'NCAAF'},
    'ncaab':  {'path': 'basketball/mens-college-basketball', 'name': 'NCAAB'},
    'mls':    {'path': 'soccer/usa.1',                       'name': 'MLS'},
    'epl':    {'path': 'soccer/eng.1',                       'name': 'EPL'},
    'laliga': {'path': 'soccer/esp.1',                       'name': 'LALIGA'},
    'ucl':    {'path': 'soccer/uefa.champions',              'name': 'UCL'},
    'wnba':   {'path': 'basketball/wnba',                    'name': 'WNBA'},
    'ncaaw':  {'path': 'basketball/womens-college-basketball','name': 'NCAAW'},
    'soft':   {'path': 'baseball/college-softball',          'name': 'SOFTBALL'},
    'msoc':   {'path': 'soccer/usa.ncaa.m.1',               'name': 'MSOC'},
    'wsoc':   {'path': 'soccer/usa.ncaa.w.1',               'name': 'WSOC'},
    'pga':    {'path': 'golf/pga',                           'name': 'PGA'},
    'ufc':    {'path': 'mma/ufc',                            'name': 'UFC'},
}
