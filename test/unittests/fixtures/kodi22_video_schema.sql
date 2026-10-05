-- Schema from CoreELEC Kodi 22 Beta2, MyVideos149. No user data.

CREATE TABLE path ( idPath integer primary key, strPath text, strContent text, strScraper text, strHash text, scanRecursive integer, useFolderNames bool, strSettings text, noUpdate bool, exclude bool, allAudio bool, dateAdded text, idParentPath integer);

CREATE TABLE files ( idFile integer primary key, idPath integer, strFilename text, playCount integer, lastPlayed text, dateAdded text);

CREATE TABLE `sets` ( idSet integer primary key, strSet text, strOverview text, strOriginalSet text);

CREATE TABLE movie ( idMovie integer primary key, idFile integer,c00 text,c01 text,c02 text,c03 text,c04 text,c05 text,c06 text,c07 text,c08 text,c09 text,c10 text,c11 text,c12 text,c13 text,c14 text,c15 text,c16 text,c17 text,c18 text,c19 text,c20 text,c21 text,c22 text,c23 text, idSet integer, userrating integer, premiered text, originalLanguage text);

CREATE TABLE videoversion (idFile INTEGER PRIMARY KEY, idMedia INTEGER, media_type TEXT, itemType INTEGER, idType INTEGER);

CREATE TABLE videoversiontype (id INTEGER PRIMARY KEY, name TEXT, owner INTEGER, itemType INTEGER);

CREATE TABLE genre ( genre_id integer primary key, name TEXT);

CREATE TABLE genre_link (genre_id integer, media_id integer, media_type TEXT);

CREATE TABLE studio ( studio_id integer primary key, name TEXT);

CREATE TABLE studio_link (studio_id integer, media_id integer, media_type TEXT);

CREATE TABLE country ( country_id integer primary key, name TEXT);

CREATE TABLE country_link (country_id integer, media_id integer, media_type TEXT);

CREATE TABLE tag (tag_id integer primary key, name TEXT);

CREATE TABLE tag_link (tag_id integer, media_id integer, media_type TEXT);

CREATE TABLE actor ( actor_id INTEGER PRIMARY KEY, name TEXT, art_urls TEXT );

CREATE TABLE actor_link(actor_id INTEGER, media_id INTEGER, media_type TEXT, role TEXT, cast_order INTEGER);

CREATE TABLE director_link(actor_id INTEGER, media_id INTEGER, media_type TEXT);

CREATE TABLE writer_link(actor_id INTEGER, media_id INTEGER, media_type TEXT);

CREATE TABLE rating (rating_id INTEGER PRIMARY KEY, media_id INTEGER, media_type TEXT, rating_type TEXT, rating FLOAT, votes INTEGER);

CREATE TABLE uniqueid (uniqueid_id INTEGER PRIMARY KEY, media_id INTEGER, media_type TEXT, value TEXT, type TEXT);

CREATE TABLE art(art_id INTEGER PRIMARY KEY, media_id INTEGER, media_type TEXT, type TEXT, url TEXT);

CREATE TABLE bookmark ( idBookmark integer primary key, idFile integer, timeInSeconds double, totalTimeInSeconds double, thumbNailImage text, player text, playerState text, type integer);

CREATE VIEW movie_view AS SELECT  movie.*,  `sets`.`strSet` AS strSet,  `sets`.`strOverview` AS strSetOverview,  `sets`.`strOriginalSet` as strOriginalSet,  files.strFileName AS strFileName,  path.strPath AS strPath,  files.playCount AS playCount,  files.lastPlayed AS lastPlayed,   files.dateAdded AS dateAdded,   bookmark.timeInSeconds AS resumeTimeInSeconds,   bookmark.totalTimeInSeconds AS totalTimeInSeconds,   bookmark.playerState AS playerState,   rating.rating AS rating,   rating.votes AS votes,   rating.rating_type AS rating_type,   uniqueid.value AS uniqueid_value,   uniqueid.type AS uniqueid_type,   EXISTS(     SELECT 1     FROM  videoversion vv     WHERE vv.idMedia = movie.idMovie     AND   vv.media_type = 'movie'     AND   vv.itemType = 1     AND   vv.idFile <> movie.idFile   ) AS hasVideoVersions,   EXISTS(     SELECT 1     FROM  videoversion vv     WHERE vv.idMedia = movie.idMovie     AND   vv.media_type = 'movie'     AND   vv.itemType = 2   ) AS hasVideoExtras,   CASE     WHEN vv.idFile = movie.idFile AND vv.itemType = 1 THEN 1     ELSE 0   END AS isDefaultVersion,   vv.idFile AS videoVersionIdFile,   vvt.id AS videoVersionTypeId,  vvt.name AS videoVersionTypeName,  vvt.itemType AS videoVersionTypeItemType FROM movie  LEFT JOIN `sets` ON    `sets`.idSet = movie.idSet  LEFT JOIN rating ON    rating.rating_id = movie.c05  LEFT JOIN uniqueid ON    uniqueid.uniqueid_id = movie.c09  LEFT JOIN videoversion vv ON    vv.idMedia = movie.idMovie AND vv.media_type = 'movie'   JOIN videoversiontype vvt ON    vvt.id = vv.idType AND vvt.itemType = vv.itemType  JOIN files ON    files.idFile = vv.idFile  JOIN path ON    path.idPath = files.idPath  LEFT JOIN bookmark ON    bookmark.idFile = vv.idFile AND bookmark.type = 1;

INSERT INTO videoversiontype (id, name, owner, itemType) VALUES (40400, 'Standard', 0, 1);
