# R Observatory

Tracking what is happening on CRAN and Bioconductor: the incoming queue, package changes, download trends across every major distribution channel, code metrics, test coverage, and metadata. Everything here is published as a rolling SQLite database, updated daily.

**[r-observatory.thecoatlessprofessor.com](https://r-observatory.thecoatlessprofessor.com/)**

## Downloads

One repo per channel, each publishing per-package counts.

- [cran-downloads](https://github.com/r-observatory/cran-downloads), daily CRAN downloads from the Posit mirror logs
- [bioconductor-downloads](https://github.com/r-observatory/bioconductor-downloads), monthly Bioconductor downloads
- [r2u-downloads](https://github.com/r-observatory/r2u-downloads), r2u, CRAN as Ubuntu binaries
- [conda-forge-downloads](https://github.com/r-observatory/conda-forge-downloads), R packages on conda-forge
- [bioconda-downloads](https://github.com/r-observatory/bioconda-downloads), R and Bioconductor packages on bioconda
- [copr-downloads](https://github.com/r-observatory/copr-downloads), Fedora COPR
- [autoobs-downloads](https://github.com/r-observatory/autoobs-downloads), openSUSE OBS
- [c2d4u-downloads](https://github.com/r-observatory/c2d4u-downloads), the Launchpad c2d4u PPAs, a frozen legacy channel

## Catalog

- [cran-metadata](https://github.com/r-observatory/cran-metadata), daily CRAN package metadata
- [bioconductor-metadata](https://github.com/r-observatory/bioconductor-metadata), the Bioconductor catalog and release lineage
- [cran-archive](https://github.com/r-observatory/cran-archive), packages removed from CRAN, with archival dates
- [cran-queue](https://github.com/r-observatory/cran-queue), hourly snapshots of the CRAN incoming queue
- [cran-feed](https://github.com/r-observatory/cran-feed), new, updated, and archived packages as they happen

## Code and quality

- [cran-code-metrics](https://github.com/r-observatory/cran-code-metrics), per-version code and quality metrics, with per-file churn
- [bioc-code-metrics](https://github.com/r-observatory/bioc-code-metrics), the same for Bioconductor releases
- [cran-coverage](https://github.com/r-observatory/cran-coverage), test coverage computed with covr in a deterministic environment
- [vcs-signals](https://github.com/r-observatory/vcs-signals), the source repository behind each package, and its activity over time

## The database

- [data](https://github.com/r-observatory/data), every pipeline above merged into one SQLite database, daily

## Tooling

- [rpkg-analyzer](https://github.com/r-observatory/rpkg-analyzer), a hermetic static analyzer for R package source trees
- [robservatory](https://github.com/r-observatory/robservatory), shared utilities used across the pipelines

## Feedback

Found a bug, a wrong number, or a missing package? Report it at [r-observatory/feedback](https://github.com/r-observatory/feedback/issues/new/choose). All feedback about R Observatory, the site, the data, and the pipelines, is tracked in one place.
