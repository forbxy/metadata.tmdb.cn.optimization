-- Relevant movie_view joins from Kodi 21 Omega's database semantics.
-- Source: https://github.com/xbmc/xbmc/blob/Omega/xbmc/video/VideoDatabase.cpp
-- VERSION=0, EXTRA=1; movie_view contains versions only.
CREATE VIEW movie_view AS
SELECT m.*, f.strFilename, p.strPath,
       CASE WHEN vv.idFile=m.idFile THEN 1 ELSE 0 END AS isDefaultVersion,
       vv.idFile AS videoVersionIdFile,
       vvt.id AS videoVersionTypeId,
       vvt.name AS videoVersionTypeName,
       vvt.itemType AS videoVersionTypeItemType
FROM movie m
JOIN videoversion vv ON vv.idMedia=m.idMovie AND vv.media_type='movie' AND vv.itemType=0
JOIN videoversiontype vvt ON vvt.id=vv.idType AND vvt.itemType=vv.itemType
JOIN files f ON f.idFile=vv.idFile
JOIN path p ON p.idPath=f.idPath;
