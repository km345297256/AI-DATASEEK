# Offline world outline

`world-outline.json` is a compact, geometry-only derivative of the Natural Earth
1:110m Admin 0 Countries layer, version **5.1.1**, already included in this
repository at `backend/app/resources/datasets/open-natural-earth-countries`.

- Publisher: Natural Earth, Tom Patterson / Nathaniel Vaughn Kelso and contributors.
- Source: https://www.naturalearthdata.com/downloads/110m-cultural-vectors/110m-admin-0-countries/
- License: Public domain; https://www.naturalearthdata.com/about/terms-of-use/
- Original `.shp` SHA-256: `08e341606e8391e458c3f08deb312de664b56bfae376064c5aa0aee6681a5f55`.

The asset contains one GeoJSON `Feature` with a `MultiLineString` geometry in
longitude/latitude degrees (WGS 84). It contains outlines only, without names or
other DBF attributes. Polygon rings were simplified with a Douglas–Peucker
tolerance of 0.15 degrees, rounded to two decimal places, and split at longitude
jumps greater than 180 degrees to avoid drawing across the antimeridian.
Small rings that would collapse below four vertices retained their original
points before rounding. Consecutive duplicate points were removed.

The resulting 289 line segments contain 7,172 coordinates derived from 10,654
source coordinates. This is a small-scale geographic reference, not an accurate
survey, a legal boundary determination, or a statement about territorial claims.
It must not be used as an analytical boundary or presented as the original data.

The file is bundled with the frontend; rendering it does not fetch map tiles,
send dataset coordinates to a third party, or require a map-service credential.
