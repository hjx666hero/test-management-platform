"""RAG 记忆模块:基于 chromadb 本地向量库的修复案例存取。

职责(全部带降级,绝不影响 Agent 主流程):
1. 记忆写入:修复成功后,把 failure_log 向量化存入本地库(附补丁/根因元数据);
2. 记忆检索:修复前,用当前 failure_log 检索 Top-3 相似历史案例,
   供 Agent 拼入 System Prompt 作 Few-shot——Agent 越修越有经验。

本地启动说明(约束要求的命令):
    chromadb 为纯嵌入式库,无需独立服务端,首次调用自动在本地创建持久化目录:
        python -c "import chromadb; chromadb.PersistentClient(path='./chroma_data')"
    (如需可选的 HTTP 模式,才用: chroma run --path ./chroma_data --port 8001)
    本模块采用 PersistentClient(进程内直连,零运维),持久化目录见 CHROMA_DIR。

降级策略:
- chromadb 未安装 / 模型下载失败 / 磁盘只读 / 检索异常 → 返回空结果并记 warning,
  Agent 拼不到 Few-shot 就当没有记忆,主流程照常;
- 所有公共函数入口即 try...except,调用方无需再包一层。
"""
import logging
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tms.agent.memory")

# 持久化目录:与后端代码同盘,进程内直连(PersistentClient)
CHROMA_DIR = str(Path(__file__).resolve().parents[2] / "chroma_data")
COLLECTION_NAME = "fix_cases"

# 单条案例文本截断(向量模型输入上限保护)
_MAX_DOC_CHARS = 4000
# 检索结果元数据截断(Few-shot prompt 体积保护)
_MAX_META_CHARS = 600

# 惰性单例:首次使用才初始化(避免 import 期就下载 embedding 模型拖慢启动)
_client = None
_collection = None
_init_failed = False  # 初始化失败标记:失败后本进程内不再反复尝试


def _get_collection():
    """惰性初始化 chroma 客户端与集合(失败则永久降级为"无记忆")。"""
    global _client, _collection, _init_failed
    if _init_failed:
        return None
    if _collection is not None:
        return _collection
    try:
        import chromadb  # 延迟导入:未安装时走降级

        _client = chromadb.PersistentClient(path=CHROMA_DIR)
        # get_or_create:首次创建,之后复用(向量化数据跨进程持久保留)
        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},  # 余弦相似度(文本语义检索常规选择)
        )
        logger.info("RAG 记忆库就绪: %s(%s 条案例)", CHROMA_DIR, _collection.count())
        return _collection
    except Exception:  # noqa: BLE001 任何初始化故障 → 静默降级
        _init_failed = True
        logger.warning("chromadb 初始化失败,RAG 记忆已降级为不可用", exc_info=True)
        return None


def remember_case(
    failure_log: str,
    case_name: str,
    file_path: str,
    patch_id: Optional[int],
    explanation: str = "",
) -> bool:
    """修复成功后写入一条记忆(failure_log 向量化入库)。

    - 文档 = failure_log(检索主键,语义相似的失败会命中);
    - 元数据 = case_name/file_path/patch_id/explanation(Few-shot 展示用);
    - 返回是否写入成功(失败只记日志,调用方无需处理)。
    """
    try:
        col = _get_collection()
        if col is None or not (failure_log or "").strip():
            return False
        col.add(
            ids=[uuid.uuid4().hex],  # chroma 要求显式 id
            documents=[failure_log[:_MAX_DOC_CHARS]],
            metadatas=[{
                "case_name": str(case_name)[:255],
                "file_path": str(file_path)[:512],
                "patch_id": int(patch_id or 0),
                "explanation": (explanation or "")[:_MAX_META_CHARS],
            }],
        )
        logger.info("RAG 记忆已写入: case=%s patch=%s", case_name, patch_id)
        return True
    except Exception:  # noqa: BLE001 记忆写入失败绝不影响主流程
        logger.warning("RAG 写入失败(已忽略): case=%s", case_name, exc_info=True)
        return False


def recall_similar(failure_log: str, top_k: int = 3) -> list:
    """检索 Top-K 相似历史修复案例(供 Few-shot)。失败/无结果返回 []。

    返回: [{case_name, file_path, explanation, similarity}, ...] 按相似度降序。
    """
    try:
        col = _get_collection()
        if col is None or not (failure_log or "").strip():
            return []
        if col.count() == 0:
            return []  # 空库:还没攒下经验,静默跳过
        res = col.query(query_texts=[failure_log[:_MAX_DOC_CHARS]], n_results=min(top_k, col.count()))
        hits = []
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for doc, meta, dist in zip(docs, metas or [None] * len(docs), dists or [1.0] * len(docs)):
            if not meta:
                continue
            hits.append({
                "failure_log": (doc or "")[:300],               # 历史失败摘要(供参考)
                "case_name": meta.get("case_name", ""),
                "file_path": meta.get("file_path", ""),
                "explanation": meta.get("explanation", ""),
                # cosine 距离 → 相似度(1-distance),仅展示用
                "similarity": round(1 - float(dist), 4) if dist is not None else None,
            })
        logger.info("RAG 检索命中 %s 条相似案例", len(hits))
        return hits
    except Exception:  # noqa: BLE001 检索失败 → 无 Few-shot,主流程照常
        logger.warning("RAG 检索失败(已忽略,跳过 Few-shot)", exc_info=True)
        return []


def build_fewshot_block(failure_log: str, top_k: int = 3) -> str:
    """把 Top-3 相似案例拼成 System Prompt 附加段(Few-shot)。

    检索失败/空库返回空串——调用方直接拼 "" 即可,零侵入。
    """
    hits = recall_similar(failure_log, top_k)
    if not hits:
        return ""
    lines = ["\n\n【历史修复经验(Few-shot)】以下是与你当前失败的相似历史案例,可参考其根因与修法:"]
    for i, h in enumerate(hits, 1):
        lines.append(
            f"{i}. 案例: {h['case_name']} | 文件: {h['file_path']}"
            f"{' | 相似度: ' + format(h['similarity'], '.0%') if h['similarity'] is not None else ''}\n"
            f"   修复结论: {h['explanation'] or '(见补丁记录)'}"
        )
    lines.append("(以上经验仅供参考,仍须用工具核实当前文件的真实内容。)")
    return "\n".join(lines)
