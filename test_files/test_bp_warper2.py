import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_MYSTIQUE_SRC = os.path.join(_SCRIPT_DIR, "mystique-opensource.github.io", "src")
sys.path.insert(0, _MYSTIQUE_SRC)

from project import Project, CodeFile
from common import Language
import difftools

def get_modified_methods(c_pa: str, c_pb: str, language: Language) -> set[str]:
    pre_proj = Project("1.pre", [CodeFile("file.c", c_pa)], language)
    post_proj = Project("2.post", [CodeFile("file.c", c_pb)], language)
    
    diff = difftools.git_diff_code(c_pa, c_pb, remove_diff_header=True)
    modified_lines = difftools.parse_diff(diff)
    
    modified_methods = set()
    for del_line in modified_lines.get("delete", []):
        for method in pre_proj.files[0].methods:
            if method.start_line <= del_line <= method.end_line:
                modified_methods.add(method.name)
                
    for add_line in modified_lines.get("add", []):
        for method in post_proj.files[0].methods:
            if method.start_line <= add_line <= method.end_line:
                modified_methods.add(method.name)
                
    return modified_methods

if __name__ == "__main__":
    print("Test loaded.")
