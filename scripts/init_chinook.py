"""
本地生成 Chinook SQLite 示例数据库（无需联网）
模拟一个音乐商店的数据结构
"""
import sqlite3
import os

DB_DIR = "data"
DB_FILE = os.path.join(DB_DIR, "chinook.sqlite")

os.makedirs(DB_DIR, exist_ok=True)

if os.path.exists(DB_FILE):
    print(f"✅ Chinook 数据库已存在: {DB_FILE}")
    exit(0)

print("🏗️  正在本地生成 Chinook 数据库...")

conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()

# ==================== 建表 ====================

cursor.executescript("""
-- 艺术家
CREATE TABLE Artist (
    ArtistId INTEGER PRIMARY KEY,
    Name NVARCHAR(120)
);

-- 专辑
CREATE TABLE Album (
    AlbumId INTEGER PRIMARY KEY,
    Title NVARCHAR(160) NOT NULL,
    ArtistId INTEGER NOT NULL,
    FOREIGN KEY (ArtistId) REFERENCES Artist(ArtistId)
);

-- 媒体类型
CREATE TABLE MediaType (
    MediaTypeId INTEGER PRIMARY KEY,
    Name NVARCHAR(120)
);

-- 曲风
CREATE TABLE Genre (
    GenreId INTEGER PRIMARY KEY,
    Name NVARCHAR(120)
);

-- 曲目
CREATE TABLE Track (
    TrackId INTEGER PRIMARY KEY,
    Name NVARCHAR(200) NOT NULL,
    AlbumId INTEGER,
    MediaTypeId INTEGER NOT NULL,
    GenreId INTEGER,
    Composer NVARCHAR(220),
    Milliseconds INTEGER NOT NULL,
    Bytes INTEGER,
    UnitPrice NUMERIC(10,2) NOT NULL,
    FOREIGN KEY (AlbumId) REFERENCES Album(AlbumId),
    FOREIGN KEY (MediaTypeId) REFERENCES MediaType(MediaTypeId),
    FOREIGN KEY (GenreId) REFERENCES Genre(GenreId)
);

-- 员工
CREATE TABLE Employee (
    EmployeeId INTEGER PRIMARY KEY,
    LastName NVARCHAR(20) NOT NULL,
    FirstName NVARCHAR(20) NOT NULL,
    Title NVARCHAR(30),
    ReportsTo INTEGER,
    BirthDate DATETIME,
    HireDate DATETIME,
    Address NVARCHAR(70),
    City NVARCHAR(40),
    State NVARCHAR(40),
    Country NVARCHAR(40),
    PostalCode NVARCHAR(10),
    Phone NVARCHAR(24),
    Fax NVARCHAR(24),
    Email NVARCHAR(60),
    FOREIGN KEY (ReportsTo) REFERENCES Employee(EmployeeId)
);

-- 客户
CREATE TABLE Customer (
    CustomerId INTEGER PRIMARY KEY,
    FirstName NVARCHAR(40) NOT NULL,
    LastName NVARCHAR(20) NOT NULL,
    Company NVARCHAR(80),
    Address NVARCHAR(70),
    City NVARCHAR(40),
    State NVARCHAR(40),
    Country NVARCHAR(40),
    PostalCode NVARCHAR(10),
    Phone NVARCHAR(24),
    Fax NVARCHAR(24),
    Email NVARCHAR(60) NOT NULL,
    SupportRepId INTEGER,
    FOREIGN KEY (SupportRepId) REFERENCES Employee(EmployeeId)
);

-- 发票
CREATE TABLE Invoice (
    InvoiceId INTEGER PRIMARY KEY,
    CustomerId INTEGER NOT NULL,
    InvoiceDate DATETIME NOT NULL,
    BillingAddress NVARCHAR(70),
    BillingCity NVARCHAR(40),
    BillingState NVARCHAR(40),
    BillingCountry NVARCHAR(40),
    BillingPostalCode NVARCHAR(10),
    Total NUMERIC(10,2) NOT NULL,
    FOREIGN KEY (CustomerId) REFERENCES Customer(CustomerId)
);

-- 发票明细
CREATE TABLE InvoiceLine (
    InvoiceLineId INTEGER PRIMARY KEY,
    InvoiceId INTEGER NOT NULL,
    TrackId INTEGER NOT NULL,
    UnitPrice NUMERIC(10,2) NOT NULL,
    Quantity INTEGER NOT NULL,
    FOREIGN KEY (InvoiceId) REFERENCES Invoice(InvoiceId),
    FOREIGN KEY (TrackId) REFERENCES Track(TrackId)
);

-- 播放列表
CREATE TABLE Playlist (
    PlaylistId INTEGER PRIMARY KEY,
    Name NVARCHAR(120)
);

-- 播放列表曲目
CREATE TABLE PlaylistTrack (
    PlaylistId INTEGER NOT NULL,
    TrackId INTEGER NOT NULL,
    PRIMARY KEY (PlaylistId, TrackId),
    FOREIGN KEY (PlaylistId) REFERENCES Playlist(PlaylistId),
    FOREIGN KEY (TrackId) REFERENCES Track(TrackId)
);
""")

# ==================== 插入数据 ====================

# 艺术家
artists = [
    (1, "AC/DC"), (2, "Accept"), (3, "Aerosmith"), (4, "Alanis Morissette"),
    (5, "Alice In Chains"), (6, "Antônio Carlos Jobim"), (7, "Apocalyptica"),
    (8, "Audioslave"), (9, "BackBeat"), (10, "Billy Cobham"),
    (11, "Black Label Society"), (12, "Black Sabbath"), (13, "Body Count"),
    (14, "Bruce Dickinson"), (15, "Buddy Guy"), (16, "Caetano Veloso"),
    (17, "Chico Buarque"), (18, "Cidade Negra"), (19, "Cláudio Zoli"),
    (20, "Various Artists"),
]
cursor.executemany("INSERT INTO Artist VALUES (?, ?)", artists)

# 专辑
albums = [
    (1, "For Those About To Rock We Salute You", 1),
    (2, "Balls to the Wall", 2),
    (3, "Restless and Wild", 2),
    (4, "Let There Be Rock", 1),
    (5, "Big Ones", 3),
    (6, "Jagged Little Pill", 4),
    (7, "Facelift", 5),
    (8, "Warner 25 Anos", 6),
    (9, "Plays Metallica By Four Cellos", 7),
    (10, "Audioslave", 8),
    (11, "The Best Of Buddy Guy", 15),
    (12, "Acústico MTV", 18),
    (13, "Quanta Gente Veio Ver", 16),
    (14, "Meus Momentos", 17),
    (15, "Chill: Brazil", 20),
]
cursor.executemany("INSERT INTO Album VALUES (?, ?, ?)", albums)

# 媒体类型
media_types = [
    (1, "MPEG audio file"),
    (2, "Protected AAC audio file"),
    (3, "Protected MPEG-4 video file"),
    (4, "Purchased AAC audio file"),
    (5, "AAC audio file"),
]
cursor.executemany("INSERT INTO MediaType VALUES (?, ?)", media_types)

# 曲风
genres = [
    (1, "Rock"), (2, "Jazz"), (3, "Metal"), (4, "Alternative & Punk"),
    (5, "Rock And Roll"), (6, "Blues"), (7, "Latin"),
    (8, "Reggae"), (9, "Pop"), (10, "Soundtrack"),
    (11, "Bossa Nova"), (12, "Easy Listening"), (13, "Heavy Metal"),
    (14, "R&B/Soul"), (15, "Electronica/Dance"), (16, "World"),
]
cursor.executemany("INSERT INTO Genre VALUES (?, ?)", genres)

# 曲目
tracks = [
    (1, "For Those About To Rock (We Salute You)", 1, 1, 1, "Angus Young, Malcolm Young, Brian Johnson", 343719, 11170334, 0.99),
    (2, "Balls to the Wall", 2, 1, 1, "U. Dirkschneider, W. Hoffmann, P. Baltes, S. Kaufmann, G. Hoffmann", 342562, 5510424, 0.99),
    (3, "Fast As a Shark", 3, 1, 1, "U. Dirkschneider, W. Hoffmann, P. Baltes, S. Kaufmann, G. Hoffmann", 230619, 3990994, 0.99),
    (4, "Restless and Wild", 3, 1, 1, "U. Dirkschneider, W. Hoffmann, P. Baltes, S. Kaufmann, G. Hoffmann", 252051, 4331779, 0.99),
    (5, "Princess of the Dawn", 3, 1, 1, "U. Dirkschneider, W. Hoffmann, P. Baltes, S. Kaufmann, G. Hoffmann", 375418, 6290521, 0.99),
    (6, "Put The Finger On You", 1, 1, 1, "Angus Young, Malcolm Young, Brian Johnson", 205662, 6713451, 0.99),
    (7, "Let's Get It Up", 1, 1, 1, "Angus Young, Malcolm Young, Brian Johnson", 233926, 7636561, 0.99),
    (8, "Inject The Venom", 1, 1, 1, "Angus Young, Malcolm Young, Brian Johnson", 210834, 6852860, 0.99),
    (9, "Snowballed", 1, 1, 1, "Angus Young, Malcolm Young, Brian Johnson", 203102, 6599424, 0.99),
    (10, "Evil Walks", 1, 1, 1, "Angus Young, Malcolm Young, Brian Johnson", 263497, 8611245, 0.99),
    (11, "C.O.D.", 1, 1, 1, "Angus Young, Malcolm Young, Brian Johnson", 199836, 6566314, 0.99),
    (12, "Breaking The Rules", 1, 1, 1, "Angus Young, Malcolm Young, Brian Johnson", 263288, 8596840, 0.99),
    (13, "Night Of The Long Knives", 1, 1, 1, "Angus Young, Malcolm Young, Brian Johnson", 205688, 6706347, 0.99),
    (14, "Spellbound", 1, 1, 1, "Angus Young, Malcolm Young, Brian Johnson", 270863, 8817038, 0.99),
    (15, "Go Down", 4, 1, 1, "AC/DC", 331180, 10847611, 0.99),
    (16, "Dog Eat Dog", 4, 1, 1, "AC/DC", 215196, 7032162, 0.99),
    (17, "Let There Be Rock", 4, 1, 1, "AC/DC", 366654, 12021261, 0.99),
    (18, "Bad Boy Boogie", 4, 1, 1, "AC/DC", 267728, 8776140, 0.99),
    (19, "Problem Child", 4, 1, 1, "AC/DC", 325041, 10617116, 0.99),
    (20, "Overdose", 4, 1, 1, "AC/DC", 369319, 12066294, 0.99),
]
cursor.executemany("INSERT INTO Track VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", tracks)

# 员工
employees = [
    (1, "Adams", "Andrew", "General Manager", None, "1962-02-18", "2002-08-14", "11120 Jasper Ave NW", "Edmonton", "AB", "Canada", "T5K 2N1", "+1 (780) 428-9482", "+1 (780) 428-3457", "andrew@chinookcorp.com"),
    (2, "Edwards", "Nancy", "Sales Manager", 1, "1958-12-08", "2002-05-01", "825 8 Ave SW", "Calgary", "AB", "Canada", "T2P 2T3", "+1 (403) 262-3443", "+1 (403) 262-3322", "nancy@chinookcorp.com"),
    (3, "Peacock", "Jane", "Sales Support Agent", 2, "1973-08-29", "2002-04-01", "1111 6 Ave SW", "Calgary", "AB", "Canada", "T2P 5M5", "+1 (403) 262-3443", "+1 (403) 262-6712", "jane@chinookcorp.com"),
    (4, "Park", "Margaret", "Sales Support Agent", 2, "1947-09-19", "2003-05-03", "683 10 Street SW", "Calgary", "AB", "Canada", "T2P 5G3", "+1 (403) 263-4423", "+1 (403) 263-4289", "margaret@chinookcorp.com"),
    (5, "Johnson", "Steve", "Sales Support Agent", 2, "1965-03-03", "2003-10-17", "7727B 41 Ave", "Calgary", "AB", "Canada", "T3B 1Y7", "1 (780) 836-9987", "1 (780) 836-9543", "steve@chinookcorp.com"),
    (6, "Mitchell", "Michael", "IT Manager", 1, "1973-07-01", "2003-10-17", "5827 Bowness Road NW", "Calgary", "AB", "Canada", "T3B 0C5", "+1 (403) 246-9887", "+1 (403) 246-9899", "michael@chinookcorp.com"),
    (7, "King", "Robert", "IT Staff", 6, "1970-05-29", "2004-01-02", "590 Columbia Boulevard West", "Lethbridge", "AB", "Canada", "T1K 5N8", "+1 (403) 456-9986", "+1 (403) 456-8485", "robert@chinookcorp.com"),
    (8, "Callahan", "Laura", "IT Staff", 6, "1968-01-09", "2004-03-04", "923 7 ST NW", "Lethbridge", "AB", "Canada", "T1H 1Y8", "+1 (403) 467-3351", "+1 (403) 467-8772", "laura@chinookcorp.com"),
]
cursor.executemany("INSERT INTO Employee VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", employees)

# 客户
customers = [
    (1, "Luís", "Gonçalves", "Embraer", "Av. Brigadeiro Faria Lima, 2170", "São José dos Campos", "SP", "Brazil", "12227-000", "+55 (12) 3923-5555", "+55 (12) 3923-5566", "luisg@embraer.com.br", 3),
    (2, "Leonie", "Köhler", None, "Theodor-Heuss-Straße 34", "Stuttgart", None, "Germany", "70174", "+49 0711 2842222", None, "leonekohler@surfeu.de", 5),
    (3, "François", "Tremblay", None, "1498 rue Bélanger", "Montréal", "QC", "Canada", "H2G 1A7", "+1 (514) 721-4711", None, "ftremblay@gmail.com", 3),
    (4, "Bjørn", "Hansen", None, "Ullevålsveien 14", "Oslo", None, "Norway", "0171", "+47 22 44 22 22", None, "bjorn.hansen@yahoo.no", 4),
    (5, "František", "Kovar", None, "Klanova 9/506", "Prague", None, "Czech Republic", "14700", "+420 2 4172 5555", None, "frantisek.kovar@frankem.net", 5),
    (6, "Helena", "Holý", None, "Rilská 3174/6", "Prague", None, "Czech Republic", "14300", "+420 2 4177 0449", None, "hholy@gmail.com", 5),
    (7, "Astrid", "Gruber", None, "Rotenturmstraße 4, 1010 Innere Stadt", "Vienne", None, "Austria", "1010", "+43 01 5134505", None, "astrid.gruber@apple.at", 5),
    (8, "Daan", "Peeters", None, "Grétrystraat 63", "Brussels", None, "Belgium", "1000", "+32 02 219 03 03", None, "daan_peeters@apple.be", 4),
    (9, "Kara", "Nielsen", None, "Sønder Boulevard 51", "Copenhagen", None, "Denmark", "1720", "+453 3331 9991", None, "kara.nielsen@jubii.dk", 4),
    (10, "Eduardo", "Martins", "Woodstock Discos", "Rua Dr. Falcão Filho, 155", "São Paulo", "SP", "Brazil", "01007-010", "+55 (11) 3033-5446", "+55 (11) 3033-4564", "eduardo@woodstock.com.br", 4),
]
cursor.executemany("INSERT INTO Customer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", customers)

# 发票
invoices = [
    (1, 2, "2024-01-01 00:00:00", "Theodor-Heuss-Straße 34", "Stuttgart", None, "Germany", "70174", 13.86),
    (2, 4, "2024-01-02 00:00:00", "Ullevålsveien 14", "Oslo", None, "Norway", "0171", 8.91),
    (3, 8, "2024-01-03 00:00:00", "Grétrystraat 63", "Brussels", None, "Belgium", "1000", 5.94),
    (4, 5, "2024-01-06 00:00:00", "Klanova 9/506", "Prague", None, "Czech Republic", "14700", 8.91),
    (5, 7, "2024-01-11 00:00:00", "Rotenturmstraße 4", "Vienne", None, "Austria", "1010", 17.82),
    (6, 10, "2024-01-19 00:00:00", "Rua Dr. Falcão Filho, 155", "São Paulo", "SP", "Brazil", "01007-010", 13.86),
    (7, 2, "2024-02-01 00:00:00", "Theodor-Heuss-Straße 34", "Stuttgart", None, "Germany", "70174", 0.99),
    (8, 1, "2024-02-01 00:00:00", "Av. Brigadeiro Faria Lima, 2170", "São José dos Campos", "SP", "Brazil", "12227-000", 3.96),
    (9, 3, "2024-02-02 00:00:00", "1498 rue Bélanger", "Montréal", "QC", "Canada", "H2G 1A7", 5.94),
    (10, 6, "2024-02-03 00:00:00", "Rilská 3174/6", "Prague", None, "Czech Republic", "14300", 8.91),
    (11, 9, "2024-02-06 00:00:00", "Sønder Boulevard 51", "Copenhagen", None, "Denmark", "1720", 13.86),
    (12, 5, "2024-02-11 00:00:00", "Klanova 9/506", "Prague", None, "Czech Republic", "14700", 3.96),
    (13, 4, "2024-02-19 00:00:00", "Ullevålsveien 14", "Oslo", None, "Norway", "0171", 0.99),
    (14, 8, "2024-03-04 00:00:00", "Grétrystraat 63", "Brussels", None, "Belgium", "1000", 0.99),
    (15, 10, "2024-03-04 00:00:00", "Rua Dr. Falcão Filho, 155", "São Paulo", "SP", "Brazil", "01007-010", 0.99),
]
cursor.executemany("INSERT INTO Invoice VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", invoices)

# 发票明细
invoice_lines = [
    (1, 1, 1, 0.99, 10),
    (2, 1, 2, 0.99, 4),
    (3, 2, 3, 0.99, 3),
    (4, 2, 4, 0.99, 6),
    (5, 3, 5, 0.99, 3),
    (6, 3, 6, 0.99, 3),
    (7, 4, 7, 0.99, 6),
    (8, 4, 8, 0.99, 3),
    (9, 5, 9, 0.99, 5),
    (10, 5, 10, 0.99, 13),
    (11, 6, 11, 0.99, 8),
    (12, 6, 12, 0.99, 6),
    (13, 7, 13, 0.99, 1),
    (14, 8, 14, 0.99, 4),
    (15, 9, 15, 0.99, 6),
    (16, 10, 16, 0.99, 9),
    (17, 11, 17, 0.99, 14),
    (18, 12, 18, 0.99, 4),
    (19, 13, 19, 0.99, 1),
    (20, 14, 20, 0.99, 1),
    (21, 15, 1, 0.99, 1),
]
cursor.executemany("INSERT INTO InvoiceLine VALUES (?, ?, ?, ?, ?)", invoice_lines)

# 播放列表
playlists = [
    (1, "Music"), (2, "Movies"), (3, "TV Shows"),
    (4, "Audiobooks"), (5, "90's Music"), (6, "Rock"),
]
cursor.executemany("INSERT INTO Playlist VALUES (?, ?)", playlists)

# 播放列表曲目
playlist_tracks = [
    (1, 1), (1, 2), (1, 3), (1, 6), (1, 15),
    (5, 1), (5, 7), (5, 9), (5, 18),
    (6, 1), (6, 2), (6, 3), (6, 4), (6, 5), (6, 15), (6, 16), (6, 17),
]
cursor.executemany("INSERT INTO PlaylistTrack VALUES (?, ?)", playlist_tracks)

conn.commit()
conn.close()

print(f"✅ Chinook 数据库生成完成: {DB_FILE}")