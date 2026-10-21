"""User-provided actual ticket results, kept once in their original order."""
import json
from pathlib import Path

PATH = Path(__file__).resolve().parent / 'data' / 'winner_results.json'

def load_results():
    return json.loads(PATH.read_text())['tickets'] if PATH.exists() else []

def result_groups(tickets, size):
    groups, incomplete, remaining = [], [], []
    for ticket in tickets:
        seq = ticket['sequence']
        for offset in range(0, len(seq), size):
            part = seq[offset:offset+size]
            item = {'ticket': ticket['id'], 'start': offset+1,
                    'end': offset+len(part), 'sequence': part}
            if len(part) < size:
                remaining.append(item)
            elif None in part:
                incomplete.append(item)
            else:
                groups.append(item)
    return groups, incomplete, remaining
