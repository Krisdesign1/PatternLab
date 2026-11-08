"""Dependency-free download formats for the application's calendar exports.

Only supplied report data is serialized. No clock, store, API, formula or model
is consulted here. Worksheet XML is streamed to ZIP entries so large selections
do not require an in-memory spreadsheet object graph beside the report.
"""
import csv
from datetime import date, datetime
from io import BytesIO, StringIO
from itertools import islice
import json
import math
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZipFile, ZIP_DEFLATED

VERSION = 'calendar-export-formats-v1'
MAX_ROWS = 1_048_576  # Excel limit, including the header.
MAX_COLUMNS = 16_384
MAX_TEXT = 32_767
COLUMNS = ('Date', 'Heure (Kinshasa)', 'Fuseau horaire', 'Coup d’envoi UTC',
           'ID du match', 'Pays', 'Compétition', 'ID de compétition', 'Saison',
           'Domicile', 'Extérieur', 'Score actuel', 'Mi-temps', 'À 90 minutes',
           'Statut', 'Code statut API', 'Minute', 'Verdict final',
           'Motif du verdict', 'Source', 'Extraction UTC')
DAY_COLUMNS = ('Date', 'Matchs stockés', 'Matchs exportés', 'État du relevé', 'Dernier relevé UTC')
PARAMETER_LABELS = {
    'start': 'Date de début incluse', 'end': 'Date de fin incluse', 'day_count': 'Journées demandées',
    'timezone': 'Fuseau horaire des journées', 'exported_at_utc': 'Extraction UTC',
    'revision': 'Révision de la base', 'store_uuid': 'Identifiant de la base',
    'leagues': 'Compétitions sélectionnées (identifiants)', 'status': 'Filtre de statut',
    'query': 'Recherche appliquée', 'stored_matches': 'Matchs stockés dans la période',
    'exported_matches': 'Matchs exportés', 'missing_days': 'Journées sans donnée locale ni relevé',
    'unconfirmed_days': 'Journées avec rencontres sans relevé daté',
    'invalid_records': 'Rencontres de la période non exportables',
    'unassignable_records': 'Rencontres sans date exploitable dans la base',
}
MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'


def _text(value):
    """Replace characters illegal in XML 1.0; preserve readable source text."""
    return ''.join(character if (character in '\t\n\r' or 0x20 <= ord(character) <= 0xD7FF
                   or 0xE000 <= ord(character) <= 0xFFFD or 0x10000 <= ord(character) <= 0x10FFFF)
                   else '\ufffd' for character in value)


def _scalar(value):
    if value is None or isinstance(value, (str, bool, int)):
        return _text(value) if isinstance(value, str) else value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Une valeur numérique de l’export est non finie.')
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple, dict)):
        try:
            return _text(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))
        except (TypeError, ValueError):
            raise ValueError('Une valeur structurée de l’export est invalide.') from None
    raise ValueError('Une valeur de l’export est d’un type non pris en charge.')


def _rows(report, key):
    if not isinstance(report, dict) or not isinstance(report.get(key), (list, tuple)):
        raise ValueError('Le rapport de calendrier est absent ou invalide.')
    rows = report[key]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError('Une ligne du rapport de calendrier est invalide.')
    return rows


def _columns(rows, requested, default):
    columns = list(requested) if requested is not None else list(default)
    if (any(not isinstance(column, str) or not column for column in columns)
            or len(set(columns)) != len(columns)):
        raise ValueError('Les colonnes de l’export sont invalides.')
    present = set(columns)
    for row in rows:
        for column in row:
            if not isinstance(column, str) or not column:
                raise ValueError('Les colonnes de l’export sont invalides.')
            if column not in present:
                columns.append(column)
                present.add(column)
    return columns


def _csv_cell(value):
    value = _scalar(value)
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'Oui' if value else 'Non'
    if isinstance(value, (int, float)):
        return str(value).replace('.', ',')
    significant = value.lstrip(' \ufeff\t\r\n\v\f')
    if significant.startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        return "'" + value
    return value


def _csv(rows, columns):
    buffer = StringIO(newline='')
    writer = csv.writer(buffer, delimiter=';', lineterminator='\r\n')
    writer.writerow([_csv_cell(column) for column in columns])
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
    return buffer.getvalue().encode('utf-8-sig')


def to_csv(report):
    """CSV of matches: UTF-8 BOM, French delimiter and precise textual dates."""
    rows = _rows(report, 'rows')
    return _csv(rows, _columns(rows, report.get('columns'), COLUMNS))


def to_coverage_csv(report):
    """Separate date-by-date evidence, including empty and unconfirmed days."""
    rows = _rows(report, 'days')
    metadata = report.get('metadata')
    if not isinstance(metadata, dict):
        raise ValueError('Les paramètres du rapport de calendrier sont absents.')
    context = {'Début de période': metadata.get('start'), 'Fin de période': metadata.get('end'),
               'Fuseau horaire': metadata.get('timezone'), 'Extraction UTC': metadata.get('exported_at_utc')}
    detached = [dict(row, **context) for row in rows]
    # Preserve a provenance-bearing header even when no coverage rows exist.
    columns = _columns(rows, report.get('day_columns'), DAY_COLUMNS)
    columns.extend(column for column in context if column not in columns)
    return _csv(detached, columns)


def _letter(index):
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell(column, row, value, *, header=False, name=None):
    value = _scalar(value)
    address = f'{_letter(column)}{row}'
    style = 1 if header else {'1st': 2, 'Égal': 3, '2nd': 4}.get(value, 0) if name == 'Verdict final' and isinstance(value, str) else 0
    style_attribute = f' s="{style}"' if style else ''
    if value is None:
        return f'<c r="{address}"{style_attribute}/>'
    if isinstance(value, bool):
        return f'<c r="{address}"{style_attribute} t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        # Excel preserves only 15 significant numeric digits. An identifier or
        # other long integer is emitted as text rather than silently rounded.
        if isinstance(value, int) and len(str(abs(value))) > 15:
            value = str(value)
        else:
            return f'<c r="{address}"{style_attribute}><v>{value}</v></c>'
    if len(value) > MAX_TEXT:
        raise ValueError('Une cellule dépasse la limite Excel de 32 767 caractères ; utilisez le CSV pour conserver son contenu.')
    return (f'<c r="{address}"{style_attribute} t="inlineStr"><is>'
            f'<t xml:space="preserve">{escape(value)}</t></is></c>')


def _width(name):
    if name == 'Paramètre':
        return 58
    if name in ('Domicile', 'Extérieur', 'Compétition', 'Pays'):
        return 27
    if name in ('Motif du verdict', 'État du relevé', 'Valeur'):
        return 48
    if 'UTC' in name or 'Fuseau' in name:
        return 28
    if name in ('Date', 'Heure (Kinshasa)', 'Score actuel', 'Mi-temps', 'À 90 minutes', 'Minute'):
        return 17
    return min(34, max(18, len(name) + 3))


def _write_sheet(archive, number, columns, rows, length):
    if len(columns) > MAX_COLUMNS:
        raise ValueError('Le tableau dépasse la limite Excel de 16 384 colonnes.')
    last = f'{_letter(len(columns))}{length + 1}' if columns else 'A1'
    with archive.open(f'xl/worksheets/sheet{number}.xml', 'w', force_zip64=True) as stream:
        def write(value):
            stream.write(value.encode('utf-8'))
        write(f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              f'<worksheet xmlns="{MAIN_NS}"><dimension ref="A1:{last}"/>'
              '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
              'activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
              '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="21"/><cols>')
        for index, column in enumerate(columns, 1):
            write(f'<col min="{index}" max="{index}" width="{_width(column)}" customWidth="1"/>')
        write('</cols><sheetData><row r="1" ht="32" customHeight="1">')
        for index, column in enumerate(columns, 1):
            write(_cell(index, 1, column, header=True))
        write('</row>')
        for row_index, row in enumerate(rows, 2):
            write(f'<row r="{row_index}">')
            for column_index, column in enumerate(columns, 1):
                write(_cell(column_index, row_index, row.get(column), name=column))
            write('</row>')
        write(f'</sheetData><autoFilter ref="A1:{last}"/>'
              '<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
              '</worksheet>')


_STYLES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{MAIN_NS}">
<fonts count="5">
<font><sz val="11"/><color rgb="FF172A43"/><name val="Aptos"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Aptos"/></font>
<font><b/><sz val="11"/><color rgb="FF175DA7"/><name val="Aptos"/></font>
<font><b/><sz val="11"/><color rgb="FF7040A0"/><name val="Aptos"/></font>
<font><b/><sz val="11"/><color rgb="FF934B08"/><name val="Aptos"/></font>
</fonts>
<fills count="6"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF172A43"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE8F2FF"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF2E9FF"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFEBD7"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="center"/></xf>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"><alignment vertical="center"/></xf>
<xf numFmtId="0" fontId="3" fillId="4" borderId="0" xfId="0" applyFont="1" applyFill="1"><alignment vertical="center"/></xf>
<xf numFmtId="0" fontId="4" fillId="5" borderId="0" xfId="0" applyFont="1" applyFill="1"><alignment vertical="center"/></xf>
</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def to_xlsx(report):
    """Styled workbook retaining all supplied matches, dates and scope metadata."""
    match_rows, day_rows = _rows(report, 'rows'), _rows(report, 'days')
    metadata = report.get('metadata')
    if not isinstance(metadata, dict):
        raise ValueError('Les paramètres du rapport de calendrier sont absents.')
    parameters = []
    for key, value in metadata.items():
        label = PARAMETER_LABELS.get(key, str(key))
        if isinstance(value, (list, tuple)) and value:
            # Long periods can contain tens of thousands of requested dates.
            # One value per row avoids Excel's per-cell text limit without
            # truncating the selection or adding redundant worksheets.
            parameters.extend({'Paramètre': f'{label} [{index}/{len(value)}]', 'Valeur': item}
                              for index, item in enumerate(value, 1))
        else:
            parameters.append({'Paramètre': label, 'Valeur': value})
    sources = [('Matchs', _columns(match_rows, report.get('columns'), COLUMNS), match_rows),
               ('Journées', _columns(day_rows, report.get('day_columns'), DAY_COLUMNS), day_rows),
               ('Paramètres', ['Paramètre', 'Valeur'], parameters)]
    if type(MAX_ROWS) is not int or MAX_ROWS < 2:
        raise ValueError('La limite de lignes Excel est invalide.')
    sheets = []
    output = BytesIO()
    with ZipFile(output, 'w', compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for title, columns, rows in sources:
            capacity = MAX_ROWS - 1
            for start in range(0, max(1, len(rows)), capacity):
                suffix = '' if start == 0 else f' {start // capacity + 1}'
                name, number = title + suffix, len(sheets) + 1
                length = min(capacity, len(rows) - start)
                _write_sheet(archive, number, columns, islice(rows, start, start + capacity), length)
                sheets.append(name)
        overrides = ''.join(f'<Override PartName="/xl/worksheets/sheet{number}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for number in range(1, len(sheets) + 1))
        archive.writestr('[Content_Types].xml',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + overrides + '</Types>')
        archive.writestr('_rels/.rels', f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{PACKAGE_REL_NS}"><Relationship Id="rId1" '
            f'Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}"><bookViews><workbookView/></bookViews><sheets>'
            + ''.join(f'<sheet name={quoteattr(name)} sheetId="{number}" r:id="rId{number}"/>'
                      for number, name in enumerate(sheets, 1)) + '</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{PACKAGE_REL_NS}">'
            + ''.join(f'<Relationship Id="rId{number}" Type="{REL_NS}/worksheet" Target="worksheets/sheet{number}.xml"/>'
                      for number in range(1, len(sheets) + 1))
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="{REL_NS}/styles" Target="styles.xml"/>'
            '</Relationships>')
        archive.writestr('xl/styles.xml', _STYLES)
    return output.getvalue()
