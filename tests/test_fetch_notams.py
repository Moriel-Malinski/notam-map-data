import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
)

from fetch_notams import parse_notams  # noqa: E402

# Mirrors the real AeroInfo layout: one tblMainInfo table per NOTAM, id and
# location only in the first row, message split across MsgText cells.
HTML_FIXTURE = """
<html><body>
<table class="tblMainInfo">
  <tr><td class="NotamID">A0123/26</td><td class="Location">LLLL</td>
      <td class="MsgText">E) TEMPORARY RESTRICTED AREA ACTIVE</td></tr>
  <tr><td class="NotamID"></td><td class="Location"></td>
      <td class="MsgText">PSN 320000N 0345000E RADIUS 2NM.)</td></tr>
</table>
<table class="tblMainInfo">
  <tr><td class="NotamID">A0124/26</td><td class="Location">LLBG</td>
      <td class="MsgText">E) RWY 08/26 CLOSED.)</td></tr>
</table>
<table class="tblMainInfo">
  <tr><td class="NotamID"></td><td class="Location"></td>
      <td class="MsgText">orphan text without an id — skipped</td></tr>
</table>
</body></html>
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
