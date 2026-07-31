import sqlite3
import pandas as pd
import sys
import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_MYSTIQUE_SRC = os.path.join(_SCRIPT_DIR, "mystique-opensource.github.io", "src")
sys.path.insert(0, _MYSTIQUE_SRC)
os.chdir(_MYSTIQUE_SRC)

from common import Language
import phase2_generate
from project import Project, CodeFile

os.chdir(_SCRIPT_DIR)
conn = phase2_generate.get_conn()
rows = phase2_generate.fetch_ready_rows(conn, overwrite=True)
row = [r for r in rows if r["id"] == 9][0]
print("ID:", row["id"])

pb_sha = phase2_generate.sha_from_url(row["new_version_patch_commit_url"])
pe_sha = phase2_generate.sha_from_url(row["old_version_patch_commit_url"])
lookup = phase2_generate.build_excel_lookup("FixMorph-Dataset/Main-data-set.xlsx")

meta = lookup.get((pb_sha, pe_sha))
if meta is None:
    meta = next(
        (v for (kpb, kpe), v in lookup.items()
         if kpb.startswith(pb_sha) or pb_sha.startswith(kpb)),
        None,
    )

pa = meta["pa"]
target_path = meta["target_path"]
ref_path = meta["ref_path"]
method_name = os.path.splitext(os.path.basename(ref_path))[0]
print("method_name:", method_name)

content = phase2_generate._cache_get(pa, target_path)
codefile = CodeFile(target_path, content)
proj = Project("1.pre", [codefile], Language.C)

print("Number of methods in file:", len(proj.files[0].methods))
if len(proj.files[0].methods) > 0:
    print("First method name:", proj.files[0].methods[0].name)
