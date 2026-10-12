"""正式运行与单测命名空间的路径及来源隔离。"""

from pathlib import Path

from investment.contracts import require


def production_root():
    return (Path.home() / ".investment").resolve()


def validate_root(root, fixture_mode=None):
    """解析真实路径后校验根类型，不创建目录或文件。"""
    resolved = Path(root).resolve()
    production = production_root()
    tests = production / "test"
    is_test = resolved != tests and resolved.is_relative_to(tests)
    require(resolved == production or is_test, "T04", "/root", "Root must be ~/.investment or an isolated child of ~/.investment/test", "use_not_allowed")
    require(fixture_mode is None or fixture_mode == is_test, "T04", "/root", "Runtime mode does not match production/test root", "use_not_allowed")
    return resolved


def validate_source_root(root, source_type):
    require(source_type in ("user_confirmation", "synthetic_fixture"), "T04", "/source_type", "Unsupported source authority", "use_not_allowed")
    return validate_root(root, fixture_mode=source_type == "synthetic_fixture")


def validate_input_path(root, input_path):
    """单测入口限本批目录，正式入口禁止读取测试目录中的文件。"""
    root = validate_root(root)
    resolved = Path(input_path).resolve()
    production = production_root()
    allowed = not resolved.is_relative_to(production / "test") if root == production else resolved.is_relative_to(root)
    require(allowed, "T04", "/input", "Input path crosses production/test namespace or test batch", "use_not_allowed")
    return resolved


def test_parent():
    """仅为单测创建约定父目录，禁止该父目录重定向。"""
    directory = production_root() / "test"
    require(directory.resolve() == directory, "T04", "/root", "Test parent must not redirect outside its declared path", "use_not_allowed")
    directory.mkdir(parents=True, exist_ok=True)
    return directory
