"""Human-readable PDF snapshots; independent of the control API's wire format."""
from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle

from .domain import CAMERAS, timestamp
from .service import status_label, STOP_LABELS

TZ = ZoneInfo('Asia/Seoul')


def time_label(value):
    try:
        return timestamp(value).astimezone(TZ).strftime('%d %b %Y, %H:%M:%S KST') if value else 'Not available'
    except (ValueError, TypeError):
        return 'Not available'


def location(camera_id):
    if not camera_id:
        return 'Camera not specified'
    name = CAMERAS.get(camera_id, {}).get('name')
    return f'Camera {camera_id}' + (f' - {name}' if name else '')


class PdfExport:
    def encode(self, case):
        buffer = BytesIO()
        # CID font supports Korean titles and notes without a machine-specific font path.
        if 'HYSMyeongJo-Medium' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont('HYSMyeongJo-Medium'))
        styles = getSampleStyleSheet()
        body = ParagraphStyle('ReportBody', fontName='HYSMyeongJo-Medium', fontSize=10,
                              leading=15, spaceAfter=6)
        story = []

        def p(text):
            return Paragraph(escape(str(text)).replace('\n', '<br/>'), body)

        def section(title):
            story.extend([Spacer(1, 10), Paragraph(title, styles['Heading2'])])

        def table(headers, rows, widths):
            if not rows:
                story.append(p('None recorded.'))
                return
            data = [[p(v) for v in headers]] + [[p(v) for v in row] for row in rows]
            grid = LongTable(data, colWidths=widths, repeatRows=1, hAlign='LEFT', splitInRow=1)
            grid.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8f1fa')),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
                ('TOPPADDING', (0, 0), (-1, -1), 7),
                ('LINEBELOW', (0, 0), (-1, -1), .4, colors.HexColor('#d5dce3')),
            ]))
            story.append(grid)

        story.append(Paragraph('Case report', styles['Title']))
        story.append(p(case.title))
        story.append(p('Generated: ' + datetime.now(TZ).strftime('%d %b %Y, %H:%M:%S KST')))
        if case.source == 'demo':
            story.append(p('Demo report: sightings and search results are simulated.'))
        story.append(p('This report is a snapshot. Downloading it does not end the search.'))
        section('Case summary')
        for label, value in [('Case ID', case.id), ('Status', status_label(case)),
                             ('Created', time_label(case.created_at)),
                             ('Last-known location', location(case.last_known.get('camera_id'))),
                             ('Last seen', time_label(case.last_known.get('time')))]:
            story.append(p(f'{label}: {value}'))
        if case.stop_reason:
            story.append(p('End reason: ' + STOP_LABELS.get(case.stop_reason, 'Not specified')))
        if case.notes:
            section('Notes')
            # Separate paragraphs permit long notes to flow across pages.
            for line in case.notes.splitlines():
                story.append(p(line))
        section('Route')
        story.append(p('Sightings supplied by the search system, in route order.'))
        table(['Location', 'Time', 'Sighting'], [
            [location(pt.get('camera_id')), time_label(pt.get('observed_at')),
             {'REPORTED': 'Reported by staff', 'CONFIRMED': 'Confirmed'}.get(pt.get('source'), 'Search result')]
            for pt in case.route], [170, 195, 146])
        section('Candidate reviews')
        rows = []
        for candidate in case.candidates:
            review = {'CONFIRMED': 'Confirmed', 'REJECTED': 'Rejected',
                      'UNREVIEWED': 'Not reviewed'}.get(candidate.decision, 'Not reviewed')
            if candidate.review_state == 'PENDING':
                review = 'Pending: ' + {'CONFIRM': 'confirmation', 'REJECT': 'rejection'}.get(candidate.requested_decision, 'review')
            rows.append([location(candidate.camera_id), time_label(candidate.observed_at), review,
                         'Long gap' if candidate.observation_type == 'LONG_GAP' else '-'])
        table(['Location', 'Observed', 'Review', 'Warning'], rows, [140, 170, 116, 85])
        if any(c.observation_type == 'LONG_GAP' for c in case.candidates):
            story.append(p('Long gap: this sighting requires staff confirmation before it can be included in the main route.'))
        section('Coverage issues')
        if not case.errors and not case.unobserved_windows:
            story.append(p('No coverage issues reported.'))
        else:
            story.append(p('Some results may be incomplete. A coverage issue does not necessarily mean the whole search failed.'))
        explanations = {
            'CAMERA_UNAVAILABLE': 'Camera temporarily unavailable. Some footage may be missing.',
            'LEGACY_SNAPSHOT': 'Records from an earlier prototype are available for reference.',
        }
        for error in case.errors:
            story.append(p(location(error.get('camera_id')) + ': ' + explanations.get(
                error.get('code'), 'A search task reported a problem. Some results may be incomplete.')))
        if case.unobserved_windows:
            table(['Location', 'Coverage missing from', 'To'], [
                [location(w.get('camera_id')), time_label(w.get('start')), time_label(w.get('end'))]
                for w in case.unobserved_windows], [171, 170, 170])

        def footer(canvas, doc):
            canvas.saveState()
            canvas.setFont('Helvetica', 9)
            canvas.setFillColor(colors.HexColor('#596574'))
            canvas.drawString(42, 25, 'Case report | All times in Korea Standard Time (UTC+09:00)')
            canvas.drawRightString(A4[0] - 42, 25, f'Page {doc.page}')
            canvas.restoreState()

        SimpleDocTemplate(buffer, pagesize=A4, rightMargin=42, leftMargin=42,
                          topMargin=36, bottomMargin=45, title='Case report').build(
                              story, onFirstPage=footer, onLaterPages=footer)
        return buffer.getvalue()
