from library_index import (format_library_metrics_message,
                           library_index_summary)
from plex_api import (format_plex_library_inventory_message,
                      format_plex_metrics_message)


def format_combined_metrics_message() -> str:
    sections: list[str] = []
    summary = library_index_summary()
    if summary.indexed_files or summary.configured_paths or summary.missing_paths:
        sections.append(format_library_metrics_message())

    sections.append(format_plex_metrics_message())
    sections.append(format_plex_library_inventory_message())
    return "\n\n".join(sections)
