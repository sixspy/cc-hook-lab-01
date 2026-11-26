from datetime import datetime, timezone
from app.db import 连接

建表 = """
CREATE TABLE IF NOT EXISTS 用户 (
  账号 TEXT PRIMARY KEY,
  密码哈希 TEXT NOT NULL,
  角色 TEXT NOT NULL DEFAULT '管理员'
);
CREATE TABLE IF NOT EXISTS 签名规 (
  键 TEXT PRIMARY KEY,
  值 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 投递端点 (
  端点键 TEXT PRIMARY KEY,
  名称 TEXT NOT NULL,
  回调URL TEXT NOT NULL,
  密钥版本 TEXT NOT NULL DEFAULT 'v1',
  启用 INTEGER NOT NULL DEFAULT 1,
  说明 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 事件 (
  事件号 TEXT PRIMARY KEY,
  事件类型 TEXT NOT NULL,
  端点键 TEXT NOT NULL DEFAULT '',
  载荷摘要 TEXT NOT NULL DEFAULT '',
  优先级 TEXT NOT NULL DEFAULT '普通',
  状态 TEXT NOT NULL DEFAULT '待投',
  入队时 TEXT NOT NULL,
  更新时 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 投递尝试 (
  尝试号 TEXT PRIMARY KEY,
  事件号 TEXT NOT NULL,
  端点键 TEXT NOT NULL,
  签名摘要 TEXT NOT NULL DEFAULT '',
  状态 TEXT NOT NULL DEFAULT '失败',
  响应码 INTEGER NOT NULL DEFAULT 0,
  说明 TEXT NOT NULL DEFAULT '',
  投递时 TEXT NOT NULL,
  FOREIGN KEY (事件号) REFERENCES 事件(事件号),
  FOREIGN KEY (端点键) REFERENCES 投递端点(端点键)
);
CREATE TABLE IF NOT EXISTS 死信 (
  死信号 TEXT PRIMARY KEY,
  事件号 TEXT NOT NULL UNIQUE,
  端点键 TEXT NOT NULL,
  失败次数 INTEGER NOT NULL DEFAULT 0,
  末次原因 TEXT NOT NULL DEFAULT '',
  状态 TEXT NOT NULL DEFAULT '待处置',
  入匣时 TEXT NOT NULL,
  FOREIGN KEY (事件号) REFERENCES 事件(事件号)
);
CREATE TABLE IF NOT EXISTS 互证批次 (
  批次号 INTEGER PRIMARY KEY AUTOINCREMENT,
  时刻 TEXT NOT NULL,
  事件总数 INTEGER NOT NULL,
  已投递数 INTEGER NOT NULL,
  死信数 INTEGER NOT NULL,
  待投数 INTEGER NOT NULL,
  是否对齐 INTEGER NOT NULL,
  说明 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 操作留痕 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  时刻 TEXT NOT NULL,
  操作 TEXT NOT NULL,
  事件号 TEXT NOT NULL DEFAULT '',
  详情 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 端点密钥 (
  端点键 TEXT NOT NULL,
  密钥版本 TEXT NOT NULL,
  版本序号 INTEGER NOT NULL DEFAULT 1,
  密钥材料 TEXT NOT NULL DEFAULT '',
  状态 TEXT NOT NULL DEFAULT '现行',
  生效时 TEXT NOT NULL,
  PRIMARY KEY (端点键, 密钥版本)
);
CREATE TABLE IF NOT EXISTS 投递物化包 (
  尝试号 TEXT PRIMARY KEY,
  事件号 TEXT NOT NULL,
  端点键 TEXT NOT NULL,
  密钥版本 TEXT NOT NULL DEFAULT '',
  版本序号 INTEGER NOT NULL DEFAULT 0,
  物化包 TEXT NOT NULL DEFAULT '',
  包摘要 TEXT NOT NULL DEFAULT '',
  签名摘要 TEXT NOT NULL DEFAULT '',
  验签状态 TEXT NOT NULL DEFAULT '',
  对端码 INTEGER NOT NULL DEFAULT 0,
  拒因 TEXT NOT NULL DEFAULT '',
  封包时 TEXT NOT NULL DEFAULT ''
);
"""

# 旧库平滑升档：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，这里幂等加列。
补列 = {
    "投递尝试": [
        "密钥版本 TEXT NOT NULL DEFAULT ''",
        "版本序号 INTEGER NOT NULL DEFAULT 0",
        "物化包摘要 TEXT NOT NULL DEFAULT ''",
        "验签状态 TEXT NOT NULL DEFAULT ''",
        "拒因 TEXT NOT NULL DEFAULT ''",
    ],
    "互证批次": [
        "密钥对账 TEXT NOT NULL DEFAULT ''",
    ],
}


def _加列若缺(conn) -> None:
    for 表, 定义们 in 补列.items():
        已有 = {r[1] for r in conn.execute(f"PRAGMA table_info({表})").fetchall()}
        for 定义 in 定义们:
            列名 = 定义.split()[0]
            if 列名 not in 已有:
                conn.execute(f"ALTER TABLE {表} ADD COLUMN {定义}")


def 版本序号(版本: str) -> int:
    digits = "".join(ch for ch in str(版本 or "") if ch.isdigit())
    return int(digits) if digits else 0


def _播种端点密钥(conn) -> None:
    """保证每个端点在密钥目录里都有一条与其当前指针一致的『现行』密钥。"""
    t = 此刻()
    eps = conn.execute("SELECT 端点键, 密钥版本 FROM 投递端点").fetchall()
    for ep in eps:
        键, 版 = ep["端点键"], ep["密钥版本"] or "v1"
        seq = 版本序号(版) or 1
        exists = conn.execute(
            "SELECT 1 FROM 端点密钥 WHERE 端点键=? AND 密钥版本=?", (键, 版)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO 端点密钥(端点键,密钥版本,版本序号,密钥材料,状态,生效时) "
                "VALUES (?,?,?,?, '现行', ?)",
                (键, 版, seq, f"hook-secret-{版}", t),
            )
        # 指针所指版本必须是唯一现行；其余降为旧钥
        conn.execute(
            "UPDATE 端点密钥 SET 状态='旧钥' WHERE 端点键=? AND 密钥版本<>?", (键, 版)
        )
        conn.execute(
            "UPDATE 端点密钥 SET 状态='现行', 版本序号=? WHERE 端点键=? AND 密钥版本=?",
            (seq, 键, 版),
        )


def _回填尝试物化包(conn) -> None:
    """把无版本/无影子包的历史尝试补齐为同票，避免迁移后被误判成账实撕裂。

    显式签于旧版本的污染票（测试/事故灌入）版本号非空，不会被改写成现行版本。
    """
    from app.services import 签名目录

    rows = conn.execute(
        "SELECT a.*, e.载荷摘要 AS 载荷摘要 FROM 投递尝试 a "
        "LEFT JOIN 事件 e ON a.事件号=e.事件号"
    ).fetchall()
    for a in rows:
        键 = a["端点键"]
        现行 = conn.execute(
            "SELECT 密钥版本 FROM 投递端点 WHERE 端点键=?", (键,)
        ).fetchone()
        现行版 = 现行["密钥版本"] if 现行 else "v1"
        版 = a["密钥版本"] or 现行版
        seq = a["版本序号"] or 版本序号(版)
        验签 = a["验签状态"]
        if not 验签:
            验签 = "通过" if a["状态"] == "成功" else "未验"
        _包, 文本, 摘要 = 签名目录.封物化包(
            尝试号=a["尝试号"],
            事件号=a["事件号"],
            端点键=键,
            密钥版本=版,
            版本序号=seq,
            签名摘要=a["签名摘要"] or "",
            载荷摘要=(a["载荷摘要"] if "载荷摘要" in a.keys() else "") or "",
        )
        if not a["密钥版本"] or not a["物化包摘要"]:
            conn.execute(
                "UPDATE 投递尝试 SET 密钥版本=?, 版本序号=?, 物化包摘要=?, 验签状态=? "
                "WHERE 尝试号=?",
                (版, seq, 摘要, 验签, a["尝试号"]),
            )
        shadow = conn.execute(
            "SELECT 1 FROM 投递物化包 WHERE 尝试号=?", (a["尝试号"],)
        ).fetchone()
        if not shadow:
            conn.execute(
                "INSERT INTO 投递物化包(尝试号,事件号,端点键,密钥版本,版本序号,物化包,"
                "包摘要,签名摘要,验签状态,对端码,拒因,封包时) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    a["尝试号"], a["事件号"], 键, 版, seq, 文本, 摘要,
                    a["签名摘要"] or "", 验签, int(a["响应码"] or 0),
                    a["拒因"] if "拒因" in a.keys() else "", a["投递时"],
                ),
            )



def 此刻() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def 初始化库() -> None:
    conn = 连接()
    conn.executescript(建表)
    _加列若缺(conn)
    if conn.execute("SELECT COUNT(*) AS c FROM 用户").fetchone()["c"] == 0:
        conn.execute("INSERT INTO 用户(账号,密码哈希,角色) VALUES (?,?,?)", ("admin", "123456", "管理员"))
    if conn.execute("SELECT COUNT(*) AS c FROM 签名规").fetchone()["c"] == 0:
        for k, v in {
            "最大重试": "3",
            "产品名": "投递台",
            "主链": "端点登记→事件入队→签名投递→失败重试与死信→投递账互证",
            "签名算法": "HMAC-SHA256",
            "超时秒": "5",
        }.items():
            conn.execute("INSERT INTO 签名规(键,值) VALUES (?,?)", (k, v))
    if conn.execute("SELECT COUNT(*) AS c FROM 投递端点").fetchone()["c"] == 0:
        for row in [
            ("ep-billing", "账单回调", "https://example.local/hooks/billing", "v1", 1, "账务"),
            ("ep-notify", "通知回调", "https://example.local/hooks/notify", "v1", 1, "通知"),
            ("ep-audit", "审计回调", "https://example.local/hooks/audit", "v2", 1, "审计"),
            ("ep-legacy", "旧版回调", "https://example.local/hooks/legacy", "v1", 0, "停用"),
            ("ep-partner", "合作方回调", "https://partner.local/ingest", "v1", 1, "外部"),
        ]:
            conn.execute(
                "INSERT INTO 投递端点(端点键,名称,回调URL,密钥版本,启用,说明) VALUES (?,?,?,?,?,?)",
                row,
            )
    if conn.execute("SELECT COUNT(*) AS c FROM 事件").fetchone()["c"] == 0:
        t = 此刻()
        for row in [
            ("EVT-demo-01", "invoice.paid", "ep-billing", "invoice=1001", "普通", "待投"),
            ("EVT-demo-02", "user.created", "ep-notify", "user=u42", "普通", "投递中"),
            ("EVT-demo-03", "order.refund", "ep-audit", "order=908", "紧急", "待投"),
            ("EVT-demo-04", "user.updated", "ep-notify", "user=u7", "低", "已投递"),
            ("EVT-demo-05", "invoice.failed", "ep-billing", "invoice=1002", "普通", "死信"),
        ]:
            conn.execute(
                "INSERT INTO 事件(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时) VALUES (?,?,?,?,?,?,?,?)",
                (*row, t, t),
            )
        conn.execute(
            "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) VALUES (?,?,?,?,?,?,?,?)",
            ("TRY-demo-01", "EVT-demo-04", "ep-notify", "sig-demo", "成功", 200, "ok", t),
        )
        conn.execute(
            "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时) VALUES (?,?,?,?,?,?,?)",
            ("DL-demo-01", "EVT-demo-05", "ep-billing", 3, "连接超时", "待处置", t),
        )
        conn.execute(
            "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
            (t, "种子", "", "初始化投递台样例"),
        )
    _播种端点密钥(conn)
    _回填尝试物化包(conn)
    conn.commit()
    conn.close()
