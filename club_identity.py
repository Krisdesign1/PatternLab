"""Causal club identities and role statistics, with conservative legacy lookup.

Known clubs are keyed by source, competition, identifier and role. Names only
resolve older rows without identifiers when the *available history* contains no
contradictory identifier. Ambiguous names never join two known clubs. Callers
must apply their historical cutoff before adding rows to this index.
"""
from collections import defaultdict


def provider_name(value):
    if value in (None, ''):
        return ''
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Source du club absente ou invalide.')
    return value.strip().casefold()


def identifier(value):
    if value in (None, ''):
        return None
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not str(value).isascii() or not str(value).isdecimal() or int(value) <= 0):
        raise ValueError('Identifiant du club absent ou invalide.')
    return str(int(value))


def fixture_identity(fixture):
    """Keywords for the Clubs estimator; labels remain available for display."""
    return dict(home_id=fixture.get('home_id'), away_id=fixture.get('away_id'),
                provider=fixture.get('provider'))


def different_clubs(home, away, home_id=None, away_id=None):
    """Confirmed IDs decide identity; legacy labels are a fallback only."""
    home_id, away_id = identifier(home_id), identifier(away_id)
    return home_id != away_id if home_id is not None and away_id is not None else home != away


def fixture_key(row, timestamp):
    """Avoid mistaking two ID-distinct, equally named fixtures for duplicates."""
    home_id, away_id = identifier(row.get('home_id')), identifier(row.get('away_id'))
    return (('id', home_id) if home_id is not None else ('name', row.get('home')),
            ('id', away_id) if away_id is not None else ('name', row.get('away')), timestamp)


def scoped_rows(rows, league, provider=None):
    """Select one source without attributing unmarked rows across mixed sources."""
    league = str(league)
    selected = [row for row in rows if str(row['league']) == league]
    known = {provider_name(row.get('provider')) for row in selected} - {''}
    target = provider_name(provider)
    if not target:
        if len(known) > 1:
            raise ValueError('Choisissez une seule source pour cette compétition.')
        target = next(iter(known), '')
    return [row for row in selected if provider_name(row.get('provider')) == target
            or not provider_name(row.get('provider')) and known <= {target}]


def _empty():
    return dict(matches=0, scored=[0, 0], conceded=[0, 0])


class ClubProfiles:
    """Incremental sufficient statistics; home and away are separate roles."""
    def __init__(self, provider=None):
        self.provider = provider_name(provider)
        self._profiles = defaultdict(_empty)
        self._name_ids = defaultdict(set)
        self._id_names = defaultdict(set)
        self._scopes = set()

    def add(self, row, goals):
        source = provider_name(row.get('provider')) or self.provider
        scope = source, str(row['league'])
        self._scopes.add(scope)
        for index, role in enumerate(('home', 'away')):
            name, team_id = row[role], identifier(row.get(f'{role}_id'))
            if team_id is not None:
                self._name_ids[(scope, name)].add(team_id)
                self._id_names[(scope, team_id)].add(name)
            key = ('id', team_id) if team_id is not None else ('legacy', name)
            value = self._profiles[(scope, role, key)]
            value['matches'] += 1
            for half in range(2):
                value['scored'][half] += goals[index][half]
                value['conceded'][half] += goals[1-index][half]

    def profile(self, name, role, *, team_id=None, league=None, provider=None):
        if role not in ('home', 'away'):
            raise ValueError('Le rôle du club doit être domicile ou extérieur.')
        team_id = identifier(team_id)
        source = provider_name(provider) or self.provider
        scopes = {scope for scope in self._scopes
                  if (league is None or scope[1] == str(league))
                  and (not source or scope[0] == source)}
        # A name-only legacy request cannot silently span competitions/sources.
        if len(scopes) != 1:
            return _empty()
        scope = next(iter(scopes))
        ids = self._name_ids.get((scope, name), set())
        if team_id is None:
            if len(ids) > 1:
                return _empty()
            team_id = next(iter(ids), None)
        keys = set()
        if team_id is not None:
            keys.add(('id', team_id))
            # Unambiguous historical aliases can bridge older ID-less records.
            keys.update(('legacy', alias) for alias in self._id_names.get((scope, team_id), ())
                        if self._name_ids[(scope, alias)] == {team_id})
        # An entirely legacy history can still serve its exact, unambiguous name.
        if not ids:
            keys.add(('legacy', name))
        value = _empty()
        for key in keys:
            current = self._profiles.get((scope, role, key), _empty())
            value['matches'] += current['matches']
            for half in range(2):
                value['scored'][half] += current['scored'][half]
                value['conceded'][half] += current['conceded'][half]
        return value
