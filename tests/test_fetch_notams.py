import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
)

from fetch_notams import (  # noqa: E402
    extract_details_script,
    parse_details_xml,
    parse_notams,
)

# Mirrors the real AeroInfo layout: one tblMainInfo table per NOTAM, wrapped
# in a divMainInfo_<internal msg number>, id and location only in the first
# row, message split across MsgText cells.
HTML_FIXTURE = """
<html><body>
<div id="divMainInfo_2035188">
<table class="tblMainInfo">
  <tr><td class="NotamID">A0123/26</td><td class="Location">LLLL</td>
      <td class="MsgText">E) TEMPORARY RESTRICTED AREA ACTIVE</td></tr>
  <tr><td class="NotamID"></td><td class="Location"></td>
      <td class="MsgText">PSN 320000N 0345000E RADIUS 2NM.)</td></tr>
</table>
</div>
<div id="divMainInfo_2035186">
<table class="tblMainInfo">
  <tr><td class="NotamID">A0124/26</td><td class="Location">LLBG</td>
      <td class="MsgText">E) RWY 08/26 CLOSED.)</td></tr>
</table>
</div>
<table class="tblMainInfo">
  <tr><td class="NotamID"></td><td class="Location"></td>
      <td class="MsgText">orphan text without an id — skipped</td></tr>
</table>
</body></html>
"""

# The script blob a details postback embeds in its response (real structure).
DETAILS_RESPONSE = """
<script type="text/javascript">
//<![CDATA[
javascript:f_buildMoreMsgInfo('<Msg MsgNumber="2035188" NotamID="A0123/26" Location="LLLL" Airfield="Tel-Aviv FIR" FromDate="202607140700" ToDate="202607141115" CreateDate="2026-07-13-07.21.05.000000" MsgType="" ToShowMoreLink="0"><MsgText>(A0123/26 NOTAMN</MsgText><MsgText>A) LLLL B) 2607140700 C) 2607141115</MsgText><MsgText>D) 0700-0745 1030-1115</MsgText><MsgText>E) TEMPORARY RESTRICTED AREA ACTIVE.)</MsgText></Msg>');//]]>
</script>
"""


def test_each_table_becomes_its_own_notam():
    notams = parse_notams(HTML_FIXTURE)
    assert [n["id"] for n in notams] == ["A0123/26", "A0124/26"]


def test_msgtext_cells_join_and_e_label_strips():
    first = parse_notams(HTML_FIXTURE)[0]
    assert first["location"] == "LLLL"
    assert first["eLine"] == (
        "TEMPORARY RESTRICTED AREA ACTIVE PSN 320000N 0345000E RADIUS 2NM."
    )
    # leading "E)" gone, trailing ")" gone
    assert not first["eLine"].startswith("E)")
    assert not first["eLine"].endswith(")")


def test_id_less_table_is_skipped():
    assert len(parse_notams(HTML_FIXTURE)) == 2


def test_msg_num_comes_from_wrapping_div():
    notams = parse_notams(HTML_FIXTURE)
    assert [n["msgNum"] for n in notams] == ["2035188", "2035186"]


def test_details_script_extraction_and_parsing():
    xml = extract_details_script(DETAILS_RESPONSE)
    assert xml is not None and xml.startswith("<Msg ")
    details = parse_details_xml(xml)
    assert details == {
        "validFrom": "2026-07-14T07:00Z",
        "validTo": "2026-07-14T11:15Z",
        "airfield": "Tel-Aviv FIR",
        "dLine": "0700-0745 1030-1115",
    }


def test_permanent_notam_dates_become_null():
    details = parse_details_xml(
        '<Msg MsgNumber="7" NotamID="A1/26" Location="LLLL" '
        'Airfield="X" FromDate="202601010000" ToDate="PERM">'
        "<MsgText>E) SOMETHING.)</MsgText></Msg>"
    )
    assert details["validFrom"] == "2026-01-01T00:00Z"
    assert details["validTo"] is None


def test_expired_marker_returns_none():
    assert parse_details_xml('<Msg MsgNumber="0"></Msg>') is None
