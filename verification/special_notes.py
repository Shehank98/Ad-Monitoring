"""Special Notes / Cost Breakdown Service.

Single source of truth for all cost calculations used in the Summary UI,
Excel export, and PDF export. Pass schedule_cost and deviated_cost from
SummaryReportMeta; call calculate() to get all derived values.
"""
from decimal import Decimal, ROUND_HALF_UP


def _d(val):
    """Convert a value to Decimal, returning Decimal('0') for None/empty."""
    if val is None or val == '':
        return Decimal('0')
    return Decimal(str(val))


def _fmt(val):
    """Format a Decimal to 2 decimal places string."""
    return f'{val:,.2f}'


class SpecialNotesService:
    """Calculate all cost breakdown values from two user-provided inputs."""

    def __init__(self, schedule_cost, deviated_cost):
        self.schedule_cost = _d(schedule_cost)
        self.deviated_cost = _d(deviated_cost)

    def calculate(self):
        sc = self.schedule_cost
        dc = self.deviated_cost

        # Left side
        net_cost                  = sc - dc
        two_point_five_percent    = (net_cost * Decimal('0.025')).quantize(Decimal('0.01'), ROUND_HALF_UP)
        total_with_tax_before_vat = net_cost + two_point_five_percent
        vat                       = (total_with_tax_before_vat * Decimal('0.18')).quantize(Decimal('0.01'), ROUND_HALF_UP)
        total_with_tax_final      = total_with_tax_before_vat + vat

        # Right side
        payable_to_media             = (sc * Decimal('0.85')).quantize(Decimal('0.01'), ROUND_HALF_UP)
        geometry_global              = (sc * Decimal('0.03')).quantize(Decimal('0.01'), ROUND_HALF_UP)
        agency_commission            = (sc * Decimal('0.12')).quantize(Decimal('0.01'), ROUND_HALF_UP)
        total_cost_to_client_88      = (sc * Decimal('0.88')).quantize(Decimal('0.01'), ROUND_HALF_UP)
        total_cost_to_client_with_vat = (total_with_tax_final * Decimal('0.88')).quantize(Decimal('0.01'), ROUND_HALF_UP)

        return {
            # Raw Decimals
            'schedule_cost':              sc,
            'deviated_cost':              dc,
            'net_cost':                   net_cost,
            'two_point_five_percent':     two_point_five_percent,
            'total_with_tax_before_vat':  total_with_tax_before_vat,
            'vat':                        vat,
            'total_with_tax_final':       total_with_tax_final,
            'payable_to_media':           payable_to_media,
            'geometry_global':            geometry_global,
            'agency_commission':          agency_commission,
            'total_cost_to_client_88':    total_cost_to_client_88,
            'total_cost_to_client_with_vat': total_cost_to_client_with_vat,

            # Pre-formatted strings (2 dp, comma thousands)
            'fmt': {
                'schedule_cost':              _fmt(sc),
                'deviated_cost':              _fmt(dc),
                'net_cost':                   _fmt(net_cost),
                'two_point_five_percent':     _fmt(two_point_five_percent),
                'total_with_tax_before_vat':  _fmt(total_with_tax_before_vat),
                'vat':                        _fmt(vat),
                'total_with_tax_final':       _fmt(total_with_tax_final),
                'payable_to_media':           _fmt(payable_to_media),
                'geometry_global':            _fmt(geometry_global),
                'agency_commission':          _fmt(agency_commission),
                'total_cost_to_client_88':    _fmt(total_cost_to_client_88),
                'total_cost_to_client_with_vat': _fmt(total_cost_to_client_with_vat),
            },
        }
