# Sources and attribution

This file records where the data Chorography consumes comes from, on what terms, and what the bundled demonstration data is.

## Federal data

Data published by agencies of the United States federal government is in the public domain and is used here on that basis. Chorography's fetchers and region builder draw on the National Weather Service API at api.weather.gov for alerts and forecasts, the United States Geological Survey earthquake service and water services for seismic and stream gauge data, the Bureau of Labor Statistics public data API for labour series, the Federal Reserve Economic Data service through its keyless fredgraph endpoint for economic series, the Census Bureau American Community Survey 5-year API for region building, and the GDELT Project's public document API for news event data. GDELT is a private research project rather than a federal agency and makes its data available for use with attribution, which this file and the ingest provenance records provide: every GDELT-derived event carries its source name and the hash of the payload it came from.

Feed etiquette is part of the design. Fetchers send a descriptive user agent naming the program, cache responses on disk and honour ETag and Last-Modified validators with conditional requests, back off on errors, and pace GDELT queries at one call per five seconds as that service requests.

## United States region data

The bundled United States region under data/usa-region was built from the Census Bureau American Community Survey 2024 5-year table-based summary files, published on www2.census.gov under programs-surveys/acs/summary_file/2024/table-based-SF, and retrieved on 2026-11-12. The tables used are B01001 (population), B19001 (household income), B19013 (median household income), B25009 (tenure by household size), B23025 (employment status), B15002 (educational attainment), B28002 (internet subscriptions) and B27001 (health insurance coverage); table B25008 (population in occupied housing units) was used during the build to reconcile the represented population and is named in the region manifest. County centroids are from the 2024 Census gazetteer county file, also on www2.census.gov. These are the published summary files rather than the Census API: the API's keyless route did not serve data on the day of the build, and the summary files carry the same estimates. The figures are survey estimates; their margins of error are published in the same files and are not carried into the region. The PMESII metrics and institution and infrastructure records in that region that are labelled template data are not Census data at all, and the region manifest says which is which.

## Map data

Real tract and place boundaries, when an operator adds them to a region directory, normally come from OpenStreetMap or from Census TIGERweb files. OpenStreetMap data is available under the Open Database License, ODbL, and regions built with it must carry the attribution credit to OpenStreetMap contributors wherever the region data is republished. TIGERweb boundary files are federal data in the public domain. The bundled sample region contains no OpenStreetMap data.

## Bundled demonstration data

Everything under data/sample-region was written for this program as demonstration and test data. The aggregate numbers were invented, the tract polygons are simple invented rectangles, and the feed fixture files were authored to the published shapes of the feeds they imitate, with invented content. The weather alert and earthquake fixtures describe events that did not happen. This data demonstrates formats and exercises the system offline; it is not a measurement of any real region and must not be cited as one.

## Reference design

The architecture follows the published description of the Synthetic Environment for Analysis and Simulations and the Sentient World Simulation concept paper by Cerri and Chaturvedi, version 2.0, dated August 22, 2006, which circulated for comment from the United States Joint Forces Command J9 and Purdue University. Chorography is an independent implementation written from that published description; no code or data from that project is used.
