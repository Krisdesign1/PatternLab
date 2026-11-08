"""Office package and CSV evidence without installing an authoring library."""
from copy import deepcopy
import csv
from io import BytesIO, StringIO
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import calendar_export_formats as export

NS = {'x': export.MAIN_NS}


def report():
    return {'columns': ['Date', 'Heure (Kinshasa)', 'Coup d’envoi UTC', 'ID du match',
                        'Domicile', 'Mi-temps', 'À 90 minutes', 'Verdict final', 'Saison', 'Manquant'],
            'rows': [
                {'Date': '2026-11-06', 'Heure (Kinshasa)': '00:00:00',
                 'Coup d’envoi UTC': '2026-11-05T23:00:00+00:00', 'ID du match': '0012345678901234567',
                 'Domicile': 'Équipe <A> & B', 'Mi-temps': '0–0', 'À 90 minutes': '0–0',
                 'Verdict final': 'Égal', 'Saison': 2026, 'Manquant': None},
                {'Date': '2026-11-07', 'Heure (Kinshasa)': '23:59:59',
                 'Coup d’envoi UTC': '2026-11-07T22:59:59+00:00', 'ID du match': '2',
                 'Domicile': '=HYPERLINK("https://example.invalid")', 'Mi-temps': '2–0',
                 'À 90 minutes': '2–1', 'Verdict final': '1st', 'Saison': 2026},
                {'Date': '2026-11-07', 'ID du match': '3', 'Domicile': '+external',
                 'Mi-temps': '0–0', 'À 90 minutes': '1–0', 'Verdict final': '2nd'}],
            'days': [
                {'Date': '2026-11-06', 'Matchs stockés': 1, 'Matchs exportés': 1,
                 'État du relevé': 'Relevé complet reçu', 'Dernier relevé UTC': '2026-11-06T22:00:00+00:00'},
                {'Date': '2026-11-07', 'Matchs stockés': 2, 'Matchs exportés': 2,
                 'État du relevé': 'Présence de matchs, relevé complet non confirmé', 'Dernier relevé UTC': None},
                {'Date': '2026-11-08', 'Matchs stockés': 0, 'Matchs exportés': 0,
                 'État du relevé': 'Aucun match stocké', 'Dernier relevé UTC': None}],
            'metadata': {'start': '2026-11-06', 'end': '2026-11-08', 'day_count': 3,
                         'timezone': 'Africa/Kinshasa', 'exported_at_utc': '2026-11-08T12:34:56+00:00',
                         'leagues': ['39', '61'], 'missing_days': ['2026-11-08'],
                         'query': '=ATTACK()', 'exported_matches': 3}}


def cell_values(xml):
    values = {}
    for cell in xml.findall('.//x:sheetData/x:row/x:c', NS):
        text = cell.find('x:is/x:t', NS)
        number = cell.find('x:v', NS)
        values[cell.attrib['r']] = text.text if text is not None else number.text if number is not None else None
    return values


class CalendarExportFormatsTests(unittest.TestCase):
    def test_csv_preserves_dates_scores_ids_missing_and_unicode(self):
        data = report()
        before = deepcopy(data)
        result = export.to_csv(data)
        self.assertTrue(result.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.DictReader(StringIO(result.decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['Date'], '2026-11-06')
        self.assertEqual(rows[0]['Coup d’envoi UTC'], '2026-11-05T23:00:00+00:00')
        self.assertEqual(rows[0]['ID du match'], '0012345678901234567')
        self.assertEqual(rows[0]['Mi-temps'], '0–0')
        self.assertEqual(rows[0]['Domicile'], 'Équipe <A> & B')
        self.assertEqual(rows[0]['Manquant'], '')
        self.assertEqual(rows[1]['Domicile'], "'" + data['rows'][1]['Domicile'])
        self.assertEqual(before, data)

    def test_csv_formula_protection_covers_whitespace_headers_and_control_prefixes(self):
        payloads = ['=1+1', '+1+1', '-1+1', '@SUM(1)', ' \ufeff\t=1', '\r@SUM(1)', '\n+1', '\ttext']
        data = {'columns': ['=header', 'Value'],
                'rows': [{'=header': text, 'Value': -3.5} for text in payloads]}
        rows = list(csv.reader(StringIO(export.to_csv(data).decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(rows[0], ["'=header", 'Value'])
        self.assertTrue(all(row[0].startswith("'") for row in rows[1:]))
        self.assertTrue(all(row[1] == '-3,5' for row in rows[1:]))

    def test_coverage_csv_keeps_empty_requested_day_and_explicit_confirmation(self):
        data = report(); before = deepcopy(data)
        rows = list(csv.DictReader(StringIO(export.to_coverage_csv(data).decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]['Date'], '2026-11-08')
        self.assertEqual(rows[-1]['Matchs exportés'], '0')
        self.assertEqual(rows[-1]['État du relevé'], 'Aucun match stocké')
        self.assertEqual(rows[-1]['Dernier relevé UTC'], '')
        for row in rows:
            self.assertEqual(row['Début de période'], '2026-11-06')
            self.assertEqual(row['Fin de période'], '2026-11-08')
            self.assertEqual(row['Fuseau horaire'], 'Africa/Kinshasa')
            self.assertEqual(row['Extraction UTC'], '2026-11-08T12:34:56+00:00')
        self.assertEqual(data, before)

    def test_xlsx_package_dates_parameters_coverage_and_verdict_styles(self):
        data = report(); before = deepcopy(data)
        with ZipFile(BytesIO(export.to_xlsx(data))) as archive:
            self.assertIsNone(archive.testzip())
            for name in archive.namelist():
                ET.fromstring(archive.read(name))
            workbook = ET.fromstring(archive.read('xl/workbook.xml'))
            self.assertEqual([sheet.attrib['name'] for sheet in workbook.findall('x:sheets/x:sheet', NS)],
                             ['Matchs', 'Journées', 'Paramètres'])
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            self.assertEqual(len(sheet.findall('x:sheetData/x:row', NS)), 4)
            self.assertEqual(sheet.find('x:sheetViews/x:sheetView/x:pane', NS).attrib['state'], 'frozen')
            self.assertEqual(sheet.find('x:autoFilter', NS).attrib['ref'], 'A1:J4')
            values = cell_values(sheet)
            self.assertEqual(values['A2'], '2026-11-06')
            self.assertEqual(values['C2'], '2026-11-05T23:00:00+00:00')
            self.assertEqual(values['D2'], '0012345678901234567')
            self.assertEqual(values['F2'], '0–0')
            self.assertEqual(values['E3'], data['rows'][1]['Domicile'])
            self.assertEqual(values['J2'], None)
            cells = {cell.attrib['r']: cell for cell in sheet.findall('.//x:sheetData/x:row/x:c', NS)}
            self.assertEqual([cells[position].attrib['s'] for position in ('H2', 'H3', 'H4')], ['3', '2', '4'])
            self.assertFalse(sheet.findall('.//x:f', NS))
            for position in ('A2', 'C2', 'D2', 'E3', 'F2'):
                self.assertEqual(cells[position].attrib['t'], 'inlineStr')
            self.assertNotIn('t', cells['I2'].attrib)
            coverage = cell_values(ET.fromstring(archive.read('xl/worksheets/sheet2.xml')))
            self.assertEqual(coverage['A4'], '2026-11-08')
            self.assertEqual(coverage['B4'], '0')
            parameters_xml = ET.fromstring(archive.read('xl/worksheets/sheet3.xml'))
            parameters = cell_values(parameters_xml)
            self.assertIn('Fuseau horaire des journées', parameters.values())
            self.assertIn('Africa/Kinshasa', parameters.values())
            self.assertIn('Compétitions sélectionnées (identifiants) [1/2]', parameters.values())
            self.assertIn('Compétitions sélectionnées (identifiants) [2/2]', parameters.values())
            self.assertIn('39', parameters.values())
            self.assertIn('61', parameters.values())
            self.assertIn('=ATTACK()', parameters.values())
            self.assertFalse(parameters_xml.findall('.//x:f', NS))
        self.assertEqual(before, data)

    def test_empty_exports_keep_headers_sheets_and_requested_date_parameters(self):
        data = report(); data['rows'] = []; data['days'] = []
        self.assertEqual(len(list(csv.reader(StringIO(export.to_csv(data).decode('utf-8-sig')), delimiter=';'))), 1)
        with ZipFile(BytesIO(export.to_xlsx(data))) as archive:
            first = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            self.assertEqual(len(first.findall('x:sheetData/x:row', NS)), 1)
            params = cell_values(ET.fromstring(archive.read('xl/worksheets/sheet3.xml')))
            self.assertIn('2026-11-06', params.values())

    def test_large_excel_selections_split_sheets_without_dropping_rows(self):
        data = {'columns': ['ID du match'], 'rows': [{'ID du match': str(value)} for value in range(7)],
                'days': [], 'metadata': {}}
        with patch.object(export, 'MAX_ROWS', 4), ZipFile(BytesIO(export.to_xlsx(data))) as archive:
            workbook = ET.fromstring(archive.read('xl/workbook.xml'))
            self.assertEqual([sheet.attrib['name'] for sheet in workbook.findall('x:sheets/x:sheet', NS)],
                             ['Matchs', 'Matchs 2', 'Matchs 3', 'Journées', 'Paramètres'])
            observed = []
            for index in (1, 2, 3):
                values = cell_values(ET.fromstring(archive.read(f'xl/worksheets/sheet{index}.xml')))
                observed.extend(value for key, value in values.items() if key != 'A1')
            self.assertEqual(observed, list(map(str, range(7))))

    def test_long_metadata_date_lists_expand_in_order_instead_of_exceeding_cell_limit(self):
        dates = [f'2026-11-07-{index:05d}' for index in range(4_000)]
        data = {'columns': ['Date'], 'rows': [], 'days': [],
                'metadata': {'start': '2026-01-01', 'missing_days': dates, 'unconfirmed_days': [],
                             'end': '2026-11-07'}}
        before = deepcopy(data)
        with ZipFile(BytesIO(export.to_xlsx(data))) as archive:
            workbook = ET.fromstring(archive.read('xl/workbook.xml'))
            self.assertEqual(len(workbook.findall('x:sheets/x:sheet', NS)), 3)
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet3.xml'))
            values = cell_values(sheet)
            self.assertEqual(len(sheet.findall('x:sheetData/x:row', NS)), len(dates) + 4)
            self.assertEqual(values['A3'], 'Journées sans donnée locale ni relevé [1/4000]')
            self.assertEqual(values['A4002'], 'Journées sans donnée locale ni relevé [4000/4000]')
            self.assertEqual([values[f'B{index + 3}'] for index in range(4_000)], dates)
            self.assertEqual(values['A4003'], 'Journées avec rencontres sans relevé daté')
            self.assertEqual(values['B4003'], '[]')
            self.assertEqual(values['A4004'], 'Date de fin incluse')
            self.assertEqual(values['B4004'], '2026-11-07')
        self.assertEqual(data, before)

    def test_empty_match_export_coverage_still_identifies_dates_scope_and_extraction(self):
        data = report(); data['rows'] = []
        rows = list(csv.DictReader(StringIO(export.to_coverage_csv(data).decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['Date'], '2026-11-06')
        self.assertEqual(rows[-1]['Date'], '2026-11-08')
        self.assertEqual(rows[-1]['Extraction UTC'], data['metadata']['exported_at_utc'])
        self.assertEqual(rows[-1]['Fuseau horaire'], data['metadata']['timezone'])

    def test_illegal_xml_characters_are_readable_and_long_text_never_silently_truncates(self):
        data = {'columns': ['Text'], 'rows': [{'Text': 'A\x00B\ud800C\tD'}], 'days': [], 'metadata': {}}
        with ZipFile(BytesIO(export.to_xlsx(data))) as archive:
            values = cell_values(ET.fromstring(archive.read('xl/worksheets/sheet1.xml')))
            self.assertEqual(values['A2'], 'A\ufffdB\ufffdC\tD')
        data['rows'][0]['Text'] = 'a' * 32_768
        with self.assertRaisesRegex(ValueError, '32 767'):
            export.to_xlsx(data)
        self.assertIn(('a' * 32_768).encode(), export.to_csv(data))

    def test_headers_append_new_fields_and_numeric_precision_is_retained(self):
        data = {'columns': ['Known'], 'rows': [{'Known': 0, 'New': 123456789012345678}],
                'days': [], 'metadata': {}}
        with ZipFile(BytesIO(export.to_xlsx(data))) as archive:
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            values = cell_values(sheet)
            self.assertEqual(values['A2'], '0')
            self.assertEqual(values['B1'], 'New')
            self.assertEqual(values['B2'], '123456789012345678')
            self.assertEqual(sheet.find('.//x:c[@r="B2"]', NS).attrib['t'], 'inlineStr')

    def test_invalid_inputs_fail_explicitly(self):
        for function, payload in ((export.to_csv, {}), (export.to_xlsx, {'rows': [], 'days': []}),
                                  (export.to_csv, {'rows': [None]}),
                                  (export.to_csv, {'columns': ['A', 'A'], 'rows': []}),
                                  (export.to_csv, {'columns': ['A'], 'rows': [{'A': float('nan')}]})):
            with self.subTest(function=function.__name__, payload=payload), self.assertRaises(ValueError):
                function(payload)


if __name__ == '__main__':
    unittest.main()
