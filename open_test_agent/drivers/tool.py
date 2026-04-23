import json
import re

class HarmonyDOMParser:
    """
    将鸿蒙平铺节点数组转换为统一的嵌套树结构。
 
    鸿蒙 DOM 特点（来自真实 hdc 数据）：
      - 顶层是 list[dict]，每个 dict 是一个扁平节点
      - 无显式父子关系字段，需通过坐标包含关系重建树
      - 坐标格式：x1/y1/x2/y2 四个独立整数字段
      - id 字段：有意义短 id（如 float_menu_anchor）
               或无意义长哈希（32位 hex，忽略）
               或空字符串（忽略）
      - selector：定位依据，优先于 id
      - 状态栏节点（WindowScene / session20 / LiveMetaBallBaseVm）
        整体剔除，不参与业务识别
    """
 
    # ── 完全丢弃的节点 type ────────────────────
    # 这些节点要么是系统级容器、要么是鸿蒙灵动岛/状态栏，
    # 对业务测试没有语义价值
    DISCARD_TYPES: set[str] = {
        "WindowScene",       # 状态栏整体容器
        "metaballNode",      # 灵动岛
        "BuilderProxyNode",  # 鸿蒙内部代理节点
        "root",              # 根占位节点（无实际内容）
    }
 
    # ── 仅在无文本/无 id 时跳过（透明容器）────
    TRANSPARENT_TYPES: set[str] = {
        "__Common__",
        "__Stack__",
        "Stack",
        "genericContainer",
        "RelativeContainer",
        "Row",
        "Flex",
    }
 
    # ── 有意义的叶子/交互节点 type ────────────
    SEMANTIC_TYPES: set[str] = {
        "Text", "staticText", "paragraph",
        "Button", "TextArea", "TextInput",
        "Image",
        "TabBar", "GridItem",
        "paragraph",
    }
 
    # 32位十六进制哈希：无意义 id，忽略
    _HASH_RE = re.compile(r"^[0-9a-f]{32}$")
 
    @classmethod
    def parse(cls, raw: list[dict]) -> dict:
        """
        主入口：平铺数组 → 嵌套树 dict（与 Android/iOS 结构对齐）
 
        返回格式与 DOMCleaner 输出完全一致：
        {
          "tag": str,
          "attrs": {
            "text": str,
            "id": str,           # 有意义的短 id
            "selector": str,     # hdc 定位用
            "bounds": str,       # "[x1,y1][x2,y2]" 统一格式
            "type": str,
          },
          "depth": int,
          "children": [...]
        }
        """
        if not raw or not isinstance(raw, list):
            return {"tag": "root", "attrs": {}, "depth": 0, "children": []}
 
        # 1. 过滤完全无用节点
        nodes = cls._filter_nodes(raw)
 
        # 2. 按面积从大到小排序（父节点面积 >= 子节点）
        nodes.sort(key=lambda n: cls._area(n), reverse=True)
 
        # 3. 用坐标包含关系重建父子关系
        tree = cls._build_tree(nodes)
 
        return tree
 
    # ── 内部方法 ──────────────────────────────
 
    @classmethod
    def _filter_nodes(cls, raw: list[dict]) -> list[dict]:
        """过滤完全无语义的节点"""
        result = []
        for node in raw:
            ntype = node.get("type", "")
            text  = (node.get("text") or "").strip()
            nid   = (node.get("id") or "").strip()
 
            # 丢弃：系统级 type
            if ntype in cls.DISCARD_TYPES:
                continue
 
            # 丢弃：面积为 0 的幽灵节点
            if cls._area(node) <= 0:
                continue
 
            # 丢弃：状态栏区域（y2 <= 137，鸿蒙状态栏高度约 136px）
            if node.get("y2", 0) <= 140 and node.get("y1", 0) < 140:
                continue
 
            # 丢弃：灵动岛（超宽但极矮，且 y1 接近 0）
            if node.get("y1", 0) < 10 and cls._area(node) > 0:
                w = node.get("x2", 0) - node.get("x1", 0)
                h = node.get("y2", 0) - node.get("y1", 0)
                if w > 400 and h < 140:
                    continue
 
            result.append(node)
        return result
 
    @classmethod
    def _build_tree(cls, nodes: list[dict]) -> dict:
        """
        用坐标包含关系构建树。
        策略：对每个节点，找面积最小且能完全包含它的节点作为父节点。
        """
        n = len(nodes)
        parent_idx = [-1] * n   # parent_idx[i] = j 表示 nodes[i] 的父是 nodes[j]
 
        for i in range(n):
            best_parent = -1
            best_area = float("inf")
            for j in range(n):
                if i == j:
                    continue
                if cls._contains(nodes[j], nodes[i]) and cls._area(nodes[j]) < best_area:
                    best_area = cls._area(nodes[j])
                    best_parent = j
            parent_idx[i] = best_parent
 
        # 找根节点（无父节点的，取面积最大的一个作为虚根）
        roots = [i for i in range(n) if parent_idx[i] == -1]
 
        # 构建子节点映射
        children_map: dict[int, list[int]] = {i: [] for i in range(n)}
        for i in range(n):
            p = parent_idx[i]
            if p != -1:
                children_map[p].append(i)
 
        def _to_dict(idx: int, depth: int) -> dict:
            node = nodes[idx]
            attrs = cls._extract_attrs(node)
            children_dicts = []
            for child_idx in children_map[idx]:
                child_dict = _to_dict(child_idx, depth + 1)
                if child_dict:
                    children_dicts.append(child_dict)
            return {
                "tag":      node.get("type", "unknown"),
                "attrs":    attrs,
                "depth":    depth,
                "children": children_dicts,
            }
 
        if not roots:
            return {"tag": "root", "attrs": {}, "depth": 0, "children": []}
 
        if len(roots) == 1:
            return _to_dict(roots[0], 0)
 
        # 多个根：套一层虚根
        return {
            "tag": "root",
            "attrs": {},
            "depth": 0,
            "children": [_to_dict(r, 1) for r in roots],
        }
 
    @classmethod
    def _extract_attrs(cls, node: dict) -> dict:
        """提取并标准化节点属性，bounds 统一为 '[x1,y1][x2,y2]' 格式"""
        attrs: dict = {}
 
        text = (node.get("text") or "").strip()
        # image 类型节点有时把图片哈希存在 text 字段，过滤掉
        if text and not cls._HASH_RE.match(text):
            attrs["text"] = text
 
        nid = (node.get("id") or "").strip()
        # 过滤掉 32 位无意义哈希 id
        if nid and not cls._HASH_RE.match(nid):
            attrs["id"] = nid
 
        selector = (node.get("selector") or "").strip()
        # selector 与 text 相同时不重复记录；以 # 开头的是 id 选择器
        if selector and selector not in ("", text):
            attrs["selector"] = selector
 
        ntype = (node.get("type") or "").strip()
        if ntype:
            attrs["type"] = ntype
 
        # 坐标 → 统一的 bounds 字符串
        x1 = node.get("x1", 0)
        y1 = node.get("y1", 0)
        x2 = node.get("x2", 0)
        y2 = node.get("y2", 0)
        if x2 > x1 and y2 > y1:
            attrs["bounds"] = f"[{x1},{y1}][{x2},{y2}]"
 
        # Button / TextArea 等可交互节点标记 clickable
        if ntype in ("Button", "TextArea", "TextInput", "TabBar", "GridItem"):
            attrs["clickable"] = "true"
 
        return attrs
 
    @classmethod
    def _area(cls, node: dict) -> int:
        w = node.get("x2", 0) - node.get("x1", 0)
        h = node.get("y2", 0) - node.get("y1", 0)
        return max(0, w) * max(0, h)
 
    @classmethod
    def _contains(cls, outer: dict, inner: dict) -> bool:
        """outer 坐标完全包含 inner（允许边界重合）"""
        return (outer["x1"] <= inner["x1"] and
                outer["y1"] <= inner["y1"] and
                outer["x2"] >= inner["x2"] and
                outer["y2"] >= inner["y2"] and
                cls._area(outer) > cls._area(inner))