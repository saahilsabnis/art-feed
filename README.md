# Weekly art feed for Aerial Views

Every Monday a GitHub Action picks about 100 public-domain paintings from the
Met and the Cleveland Museum of Art (European paintings, Japanese screens and
Indian paintings), resizes them to fit a 4K screen, and publishes them here
with GitHub Pages. Paintings used last week are skipped, so each week is new.

- Aerial Views feed: `https://<your-username>.github.io/art-feed/feed.csv`
- Browse this week's set: `https://<your-username>.github.io/art-feed/`

## Change the mix
Edit `sources.json`: `count` is how many paintings each source contributes;
`min_ratio`/`max_ratio` limit the shape (width ÷ height). Keep the total around
100–150 so the whole set fits in Aerial Views' 250 MB image cache.

## Run it now
Actions tab → Weekly art feed → Run workflow, or `gh workflow run build.yml`.
If a run fails, last week's paintings stay up.
