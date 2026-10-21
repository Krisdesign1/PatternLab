"""Paginated PDF export of the complete selected scenario ranking."""
from io import BytesIO
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle


def scenario_pdf(rows, length, source, mode, window, odds):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=30,
                            leftMargin=30, topMargin=30, bottomMargin=35,
                            title=f'PatternLab - Scénarios de {length} matchs')
    styles = getSampleStyleSheet()
    cell = ParagraphStyle('ScenarioCell', fontName='Helvetica', fontSize=9, leading=13)
    def p(text):
        return Paragraph(escape(str(text)), cell)
    story = [Paragraph(f'PatternLab - Scénarios de {length} matchs', styles['Title']),
             p(f'Source : {source} | Analyse : {mode} | Fenêtre : {window}'),
             p('Cotes de référence : ' + ' ; '.join(f'{k} = {v:.2f}' for k, v in odds.items())),
             p(f'Classement complet : {len(rows)} scénarios. Fréquences historiques, pas des probabilités de réussite. '
               'Cotes combinées indicatives, hors bonus et plafonds. 1st = plus de buts en première période ; '
               'Égal = autant dans les deux périodes ; 2nd = plus en deuxième période.'), Spacer(1, 14)]
    data = [[p(x) for x in ['N°', 'Scénario (ordre des matchs)', 'Occurrences', 'Part', 'Cote combinée']]]
    for index, row in enumerate(rows, 1):
        data.append([p(index), p(row['scénario'].replace(' → ', ' > ')), p(row['occurrences']),
                     p(f'{row["part_des_occurrences_%"]:.2f} %'),
                     p(f'{row["cote_combinee_indicative"]:,.2f}'.replace(',', ' '))])
    table = LongTable(data, colWidths=[30, 430, 75, 65, doc.width-600], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#DCE6F1')),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F3F6FA')]),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#CDD5DF')),
        ('TOPPADDING', (0,0), (-1,-1), 7), ('BOTTOMPADDING', (0,0), (-1,-1), 7),
    ]))
    story.append(table)
    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.drawRightString(document.pagesize[0]-30, 18, f'PatternLab | Page {document.page}')
        canvas.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
