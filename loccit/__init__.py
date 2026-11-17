"""loccit: check the citations of a thesis against the cited papers."""

import pymupdf

__version__ = "0.1.0"

# MuPDF prints problems with damaged PDFs straight to the terminal, also when
# it can read the text just fine. loccit keeps them out of the terminal and
# logs them per paper instead (see loccit.sentences).
pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)
