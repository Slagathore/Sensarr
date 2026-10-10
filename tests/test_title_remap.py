# =============================================================================
# Files numbered by a pack's own scheme are matched to episodes by title.
# The Man vs Wild pack splits the broadcaster's 15-episode season 1 into 9 + 6:
# its "S02E01 Everglades" is TVDB/TMDB S01E10 "Everglades" (2026-10-10).
# =============================================================================

import show_tracker


def _ep(season, episode, title):
    return type("E", (), {"season": season, "episode": episode, "title": title})()


TRACKER = [_ep(1, 1, "The Rockies"), _ep(1, 4, "European Alps"),
           _ep(1, 8, "Alaskan Mountain Range"), _ep(1, 10, "Everglades"),
           _ep(1, 11, "Iceland"), _ep(2, 1, "Sahara"), _ep(2, 2, "Desert Survivor")]


def test_pack_numbering_is_mapped_by_title():
    disk = {(1, 1): "S1/Man.vs.Wild.S01E01.Rockies.avi",
            (1, 4): "S1/Man.vs.Wild.S01E04.Alaskan.Mountain.Range.avi",
            (1, 8): "S1/Man.vs.Wild.S01E08.European.Alps.avi",
            (2, 1): "S2/Man.vs.Wild.s02e01.Everglades - Florida.avi",
            (2, 2): "S2/Man.vs.Wild.s02e02.Iceland.avi"}
    mapped, moved = show_tracker._remap_disk_by_titles(disk, TRACKER)
    assert mapped[(1, 10)].endswith("Everglades - Florida.avi")
    assert mapped[(1, 11)].endswith("Iceland.avi")
    assert mapped[(1, 4)].endswith("European.Alps.avi")      # swapped pair
    assert mapped[(1, 8)].endswith("Alaskan.Mountain.Range.avi")
    assert mapped[(1, 1)].endswith("Rockies.avi")            # already agreed
    assert (2, 1) not in mapped and (2, 2) not in mapped
    assert moved == 4


def test_files_without_titles_keep_their_numbers():
    disk = {(2, 1): "American Dad - S02E01.mkv"}
    mapped, moved = show_tracker._remap_disk_by_titles(disk, TRACKER)
    assert mapped == disk and moved == 0


def test_ambiguous_titles_do_not_move():
    tracker = [_ep(1, 1, "Pilot"), _ep(2, 1, "Panama (1)"), _ep(2, 2, "Panama (2)")]
    disk = {(3, 1): "Show.S03E01.Panama.avi"}
    mapped, moved = show_tracker._remap_disk_by_titles(disk, tracker)
    assert mapped == disk and moved == 0


def test_a_slot_already_held_by_an_agreeing_file_is_not_taken():
    tracker = [_ep(1, 1, "Everglades"), _ep(1, 2, "Iceland")]
    disk = {(1, 1): "Show.S01E01.Everglades.avi",
            (2, 5): "Show.S02E05.Everglades.Repack.avi"}
    mapped, moved = show_tracker._remap_disk_by_titles(disk, tracker)
    assert mapped[(1, 1)].endswith("S01E01.Everglades.avi")
    assert moved == 0
