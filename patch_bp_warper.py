import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_MYSTIQUE_SRC = os.path.join(_SCRIPT_DIR, "mystique-opensource.github.io", "src")
sys.path.insert(0, _MYSTIQUE_SRC)

from project import Project, CodeFile
from common import Language
import difftools

def get_bp_warper_code():
    return """
def bp_warper(cveid: str, patch: dict[str, str], file_path: str, method_name: str, language: Language, overwrite: bool = False, slice_level: int = config.SLICE_LEVEL) -> dict[str, str | list[int]]:
    try:
        from project import Project, CodeFile
        import difftools
        
        c_pa = patch["origin_before_func_code"]
        c_pb = patch["origin_after_func_code"]
        
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
                    
        if not modified_methods:
            return {
                "cveid": cveid,
                "file_path": file_path,
                "method_name": method_name,
                "error": ErrorCode.METHOD_NOT_FOUND.value
            }
            
        patched_bodies = {}
        total_usage = None
        
        for m_name in modified_methods:
            res = bp(cveid, patch, file_path, m_name, language, overwrite, slice_level)
            if res.get("error") != ErrorCode.SUCCESS.value:
                # If any method fails, return the error immediately
                return res
            patched_bodies[m_name] = res["fixed_code"]
            
            if total_usage is None:
                total_usage = res["usage"]
            else:
                total_usage.calls += res["usage"].calls
                total_usage.input_tokens += res["usage"].input_tokens
                total_usage.output_tokens += res["usage"].output_tokens
                total_usage.reasoning_tokens += res["usage"].reasoning_tokens
                total_usage.total_tokens += res["usage"].total_tokens
                
        target_code = patch["target_before_func_code"]
        target_proj = Project("3.target", [CodeFile("file.c", target_code)], language)
        
        methods_to_replace = []
        for m_name in modified_methods:
            sig = f"{file_path.split('/')[-1]}#{m_name}"
            tm = target_proj.get_method(sig)
            if tm is None:
                return {"error": ErrorCode.METHOD_NOT_FOUND.value}
            methods_to_replace.append((tm.node.start_byte, tm.node.end_byte, patched_bodies[m_name]))
            
        methods_to_replace.sort(key=lambda x: x[0], reverse=True)
        full_target_bytes = target_code.encode('utf-8')
        for start_byte, end_byte, new_body in methods_to_replace:
            full_target_bytes = full_target_bytes[:start_byte] + new_body.encode('utf-8') + full_target_bytes[end_byte:]
            
        full_target_code = full_target_bytes.decode('utf-8')
        
        final_diff = difftools.git_diff_code(target_code, full_target_code, remove_diff_header=False, context="normal")
        lines = final_diff.splitlines()
        if len(lines) >= 4 and lines[2].startswith("---") and lines[3].startswith("+++"):
            lines[2] = f"--- a/{file_path}"
            lines[3] = f"+++ b/{file_path}"
            final_diff = "\\n".join(lines) + "\\n"
            
        return {
            "error": ErrorCode.SUCCESS.value,
            "fixed_code": final_diff,
            "usage": total_usage,
            "cveid": cveid,
            "file_path": file_path,
            "method_name": method_name
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "cveid": cveid,
            "file_path": file_path,
            "method_name": method_name,
            "error": ErrorCode.EXCEPTION.value
        }
"""
