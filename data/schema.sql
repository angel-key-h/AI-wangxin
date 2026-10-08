-- ============================================================================
-- 多模态数据驱动的可解释精准问数/问答智能体 —— 数据库 schema 定义
-- 赛题八（中电云计算技术有限公司）· 「中国电子杯」第三届高校 ICT 产教融合创新大赛
-- ============================================================================
-- 本文件由 scripts/export_schema.py 从 data/agent.sqlite 真实导出，不是手写整理，
-- 因此与实际库结构保证一致（库结构变更后重跑该脚本即可）。
--
-- SQLite 不支持 COMMENT ON，故每张表的来源与行数写成 SQL 注释。
-- 库由两条来源合成：
--   1. Chinook 示例库（官方 github.com/lerocha/chinook-database，完整 11 张表）
--      —— 用于演示多表关联、聚合、嵌套查询
--   2. 从 rag_data/ 下的结构化文件（xlsx / csv）按表自动建表
--      —— 用于演示「非结构化文件 → 结构化问数」的链路
--
-- 连接方式：只读（见 tools/sql_tool.py，URI mode=ro），
-- 防止 LLM 生成的 SQL 意外写入生产数据。
-- ============================================================================
-- 共 16 张表

-- 来源: Chinook 示例库；行数: 347；列数: 3
CREATE TABLE IF NOT EXISTS [Album]
(
    [AlbumId] INTEGER  NOT NULL,
    [Title] NVARCHAR(160)  NOT NULL,
    [ArtistId] INTEGER  NOT NULL,
    CONSTRAINT [PK_Album] PRIMARY KEY  ([AlbumId]),
    FOREIGN KEY ([ArtistId]) REFERENCES [Artist] ([ArtistId]) 
		ON DELETE NO ACTION ON UPDATE NO ACTION
);
-- 字段: AlbumId、Title、ArtistId

-- 来源: Chinook 示例库；行数: 275；列数: 2
CREATE TABLE IF NOT EXISTS [Artist]
(
    [ArtistId] INTEGER  NOT NULL,
    [Name] NVARCHAR(120),
    CONSTRAINT [PK_Artist] PRIMARY KEY  ([ArtistId])
);
-- 字段: ArtistId、Name

-- 来源: Chinook 示例库；行数: 59；列数: 13
CREATE TABLE IF NOT EXISTS [Customer]
(
    [CustomerId] INTEGER  NOT NULL,
    [FirstName] NVARCHAR(40)  NOT NULL,
    [LastName] NVARCHAR(20)  NOT NULL,
    [Company] NVARCHAR(80),
    [Address] NVARCHAR(70),
    [City] NVARCHAR(40),
    [State] NVARCHAR(40),
    [Country] NVARCHAR(40),
    [PostalCode] NVARCHAR(10),
    [Phone] NVARCHAR(24),
    [Fax] NVARCHAR(24),
    [Email] NVARCHAR(60)  NOT NULL,
    [SupportRepId] INTEGER,
    CONSTRAINT [PK_Customer] PRIMARY KEY  ([CustomerId]),
    FOREIGN KEY ([SupportRepId]) REFERENCES [Employee] ([EmployeeId]) 
		ON DELETE NO ACTION ON UPDATE NO ACTION
);
-- 字段: CustomerId、FirstName、LastName、Company、Address、City、State、Country、PostalCode、Phone、Fax、Email、SupportRepId

-- 来源: Chinook 示例库；行数: 8；列数: 15
CREATE TABLE IF NOT EXISTS [Employee]
(
    [EmployeeId] INTEGER  NOT NULL,
    [LastName] NVARCHAR(20)  NOT NULL,
    [FirstName] NVARCHAR(20)  NOT NULL,
    [Title] NVARCHAR(30),
    [ReportsTo] INTEGER,
    [BirthDate] DATETIME,
    [HireDate] DATETIME,
    [Address] NVARCHAR(70),
    [City] NVARCHAR(40),
    [State] NVARCHAR(40),
    [Country] NVARCHAR(40),
    [PostalCode] NVARCHAR(10),
    [Phone] NVARCHAR(24),
    [Fax] NVARCHAR(24),
    [Email] NVARCHAR(60),
    CONSTRAINT [PK_Employee] PRIMARY KEY  ([EmployeeId]),
    FOREIGN KEY ([ReportsTo]) REFERENCES [Employee] ([EmployeeId]) 
		ON DELETE NO ACTION ON UPDATE NO ACTION
);
-- 字段: EmployeeId、LastName、FirstName、Title、ReportsTo、BirthDate、HireDate、Address、City、State、Country、PostalCode、Phone、Fax、Email

-- 来源: Chinook 示例库；行数: 25；列数: 2
CREATE TABLE IF NOT EXISTS [Genre]
(
    [GenreId] INTEGER  NOT NULL,
    [Name] NVARCHAR(120),
    CONSTRAINT [PK_Genre] PRIMARY KEY  ([GenreId])
);
-- 字段: GenreId、Name

-- 来源: Chinook 示例库；行数: 412；列数: 9
CREATE TABLE IF NOT EXISTS [Invoice]
(
    [InvoiceId] INTEGER  NOT NULL,
    [CustomerId] INTEGER  NOT NULL,
    [InvoiceDate] DATETIME  NOT NULL,
    [BillingAddress] NVARCHAR(70),
    [BillingCity] NVARCHAR(40),
    [BillingState] NVARCHAR(40),
    [BillingCountry] NVARCHAR(40),
    [BillingPostalCode] NVARCHAR(10),
    [Total] NUMERIC(10,2)  NOT NULL,
    CONSTRAINT [PK_Invoice] PRIMARY KEY  ([InvoiceId]),
    FOREIGN KEY ([CustomerId]) REFERENCES [Customer] ([CustomerId]) 
		ON DELETE NO ACTION ON UPDATE NO ACTION
);
-- 字段: InvoiceId、CustomerId、InvoiceDate、BillingAddress、BillingCity、BillingState、BillingCountry、BillingPostalCode、Total

-- 来源: Chinook 示例库；行数: 2240；列数: 5
CREATE TABLE IF NOT EXISTS [InvoiceLine]
(
    [InvoiceLineId] INTEGER  NOT NULL,
    [InvoiceId] INTEGER  NOT NULL,
    [TrackId] INTEGER  NOT NULL,
    [UnitPrice] NUMERIC(10,2)  NOT NULL,
    [Quantity] INTEGER  NOT NULL,
    CONSTRAINT [PK_InvoiceLine] PRIMARY KEY  ([InvoiceLineId]),
    FOREIGN KEY ([InvoiceId]) REFERENCES [Invoice] ([InvoiceId]) 
		ON DELETE NO ACTION ON UPDATE NO ACTION,
    FOREIGN KEY ([TrackId]) REFERENCES [Track] ([TrackId]) 
		ON DELETE NO ACTION ON UPDATE NO ACTION
);
-- 字段: InvoiceLineId、InvoiceId、TrackId、UnitPrice、Quantity

-- 来源: Chinook 示例库；行数: 5；列数: 2
CREATE TABLE IF NOT EXISTS [MediaType]
(
    [MediaTypeId] INTEGER  NOT NULL,
    [Name] NVARCHAR(120),
    CONSTRAINT [PK_MediaType] PRIMARY KEY  ([MediaTypeId])
);
-- 字段: MediaTypeId、Name

-- 来源: Chinook 示例库；行数: 18；列数: 2
CREATE TABLE IF NOT EXISTS [Playlist]
(
    [PlaylistId] INTEGER  NOT NULL,
    [Name] NVARCHAR(120),
    CONSTRAINT [PK_Playlist] PRIMARY KEY  ([PlaylistId])
);
-- 字段: PlaylistId、Name

-- 来源: Chinook 示例库；行数: 8715；列数: 2
CREATE TABLE IF NOT EXISTS [PlaylistTrack]
(
    [PlaylistId] INTEGER  NOT NULL,
    [TrackId] INTEGER  NOT NULL,
    CONSTRAINT [PK_PlaylistTrack] PRIMARY KEY  ([PlaylistId], [TrackId]),
    FOREIGN KEY ([PlaylistId]) REFERENCES [Playlist] ([PlaylistId]) 
		ON DELETE NO ACTION ON UPDATE NO ACTION,
    FOREIGN KEY ([TrackId]) REFERENCES [Track] ([TrackId]) 
		ON DELETE NO ACTION ON UPDATE NO ACTION
);
-- 字段: PlaylistId、TrackId

-- 来源: Chinook 示例库；行数: 3503；列数: 9
CREATE TABLE IF NOT EXISTS [Track]
(
    [TrackId] INTEGER  NOT NULL,
    [Name] NVARCHAR(200)  NOT NULL,
    [AlbumId] INTEGER,
    [MediaTypeId] INTEGER  NOT NULL,
    [GenreId] INTEGER,
    [Composer] NVARCHAR(220),
    [Milliseconds] INTEGER  NOT NULL,
    [Bytes] INTEGER,
    [UnitPrice] NUMERIC(10,2)  NOT NULL,
    CONSTRAINT [PK_Track] PRIMARY KEY  ([TrackId]),
    FOREIGN KEY ([AlbumId]) REFERENCES [Album] ([AlbumId]) 
		ON DELETE NO ACTION ON UPDATE NO ACTION,
    FOREIGN KEY ([GenreId]) REFERENCES [Genre] ([GenreId]) 
		ON DELETE NO ACTION ON UPDATE NO ACTION,
    FOREIGN KEY ([MediaTypeId]) REFERENCES [MediaType] ([MediaTypeId]) 
		ON DELETE NO ACTION ON UPDATE NO ACTION
);
-- 字段: TrackId、Name、AlbumId、MediaTypeId、GenreId、Composer、Milliseconds、Bytes、UnitPrice

-- 来源: rag_data/finance_JPMORGAN_2022_10K.xlsx（由 rag_data 结构化文件自动建表）；行数: 9；列数: 3
CREATE TABLE IF NOT EXISTS "finance_JPMORGAN_2022_10K_page0" (
"Title of each class" TEXT,
  "Trading Symbol(s)" TEXT,
  "Name of each exchange on which registered" TEXT
);
-- 字段: Title of each class、Trading Symbol(s)、Name of each exchange on which registered

-- 来源: rag_data/finance_JPMORGAN_2022_10K.xlsx（由 rag_data 结构化文件自动建表）；行数: 6；列数: 2
CREATE TABLE IF NOT EXISTS "finance_JPMORGAN_2022_10K_page3" (
"Employee Breakdown by Region" TEXT,
  "列2" TEXT
);
-- 字段: Employee Breakdown by Region、列2

-- 来源: rag_data/finance_JPMORGAN_2023Q2_10Q.csv（由 rag_data 结构化文件自动建表）；行数: 49；列数: 16
CREATE TABLE IF NOT EXISTS "finance_JPMORGAN_2023Q2_10Q" (
"\multirow[t]{2}{*}{As of or for the period ended, (in millions, except per share, ratio, headcount data and where otherwise noted)}" TEXT,
  "\multirow[b]{2}{*}{2Q23}" TEXT,
  "Unnamed: 2" TEXT,
  "\multirow[b]{2}{*}{1Q23}" TEXT,
  "\multirow[b]{2}{*}{4Q22}" TEXT,
  "\multirow[b]{2}{*}{3Q22}" TEXT,
  "\multirow[b]{2}{*}{2Q22}" TEXT,
  "Six months ended June 30," TEXT,
  "Unnamed: 8" TEXT,
  "Unnamed: 9" TEXT,
  "Unnamed: 10" TEXT,
  "Unnamed: 11" TEXT,
  "Unnamed: 12" TEXT,
  "Unnamed: 13" TEXT,
  "Unnamed: 14" TEXT,
  "Unnamed: 15" TEXT
);
-- 字段: \multirow[t]{2}{*}{As of or for the period ended, (in millions, except per share, ratio, headcount data and where otherwise noted)}、\multirow[b]{2}{*}{2Q23}、Unnamed: 2、\multirow[b]{2}{*}{1Q23}、\multirow[b]{2}{*}{4Q22}、\multirow[b]{2}{*}{3Q22}、\multirow[b]{2}{*}{2Q22}、Six months ended June 30,、Unnamed: 8、Unnamed: 9、Unnamed: 10、Unnamed: 11、Unnamed: 12、Unnamed: 13、Unnamed: 14、Unnamed: 15

-- 来源: rag_data/law_ArmstrongFlooringInc_20190107_8-K_EX-10.2_11471795_EX-10.2_Intellectual_Prop.xlsx（由 rag_data 结构化文件自动建表）；行数: 1；列数: 2
CREATE TABLE IF NOT EXISTS "law_ArmstrongFlooringInc_20190107_8K_EX1" (
"说明" TEXT,
  "内容" TEXT
);
-- 字段: 说明、内容

-- 来源: rag_data/【电子学院2025级电子工程类综测素质测评结果】综合素质测评情况.xlsx（由 rag_data 结构化文件自动建表）；行数: 569；列数: 23
CREATE TABLE IF NOT EXISTS "综合素质测评情况" (
"学号" INTEGER,
  "姓名" TEXT,
  "学院" TEXT,
  "年级" INTEGER,
  "专业" TEXT,
  "行政班" INTEGER,
  "参评人数" INTEGER,
  "综合素质测评成绩" REAL,
  "综合素质测评成绩排名" INTEGER,
  "德育测评分" REAL,
  "德育测评分排名" INTEGER,
  "智育测评分" REAL,
  "智育测评分排名" INTEGER,
  "体育测评分" REAL,
  "体育测评分排名" INTEGER,
  "美育测评分" REAL,
  "美育测评分排名" INTEGER,
  "劳动教育测评分" REAL,
  "劳动教育测评分排名" INTEGER,
  "学术科技与创新创业实践分" REAL,
  "学术科技与创新创业实践分排名" INTEGER,
  "扣分" REAL,
  "扣分排名" INTEGER
);
-- 字段: 学号、姓名、学院、年级、专业、行政班、参评人数、综合素质测评成绩、综合素质测评成绩排名、德育测评分、德育测评分排名、智育测评分、智育测评分排名、体育测评分、体育测评分排名、美育测评分、美育测评分排名、劳动教育测评分、劳动教育测评分排名、学术科技与创新创业实践分、学术科技与创新创业实践分排名、扣分、扣分排名

-- ----------------------------------------------------------------------------
-- 表来源清单
-- ----------------------------------------------------------------------------
-- [Chinook 基底库] 11 张：Album、Artist、Customer、Employee、Genre、Invoice、InvoiceLine、MediaType、Playlist、PlaylistTrack、Track
-- [rag_data 文件转表] 5 张：
--   finance_JPMORGAN_2022_10K_page0  ←  rag_data/finance_JPMORGAN_2022_10K.xlsx  （9 行 × 3 列）
--   finance_JPMORGAN_2022_10K_page3  ←  rag_data/finance_JPMORGAN_2022_10K.xlsx  （6 行 × 2 列）
--   finance_JPMORGAN_2023Q2_10Q  ←  rag_data/finance_JPMORGAN_2023Q2_10Q.csv  （49 行 × 16 列）
--   law_ArmstrongFlooringInc_20190107_8K_EX1  ←  rag_data/law_ArmstrongFlooringInc_20190107_8-K_EX-10.2_11471795_EX-10.2_Intellectual_Prop.xlsx  （1 行 × 2 列）
--   综合素质测评情况  ←  rag_data/【电子学院2025级电子工程类综测素质测评结果】综合素质测评情况.xlsx  （569 行 × 23 列）

-- 合计 16 张表 / 16241 行
