import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Check, Download, Star, Tag } from 'lucide-react'
import {
  api,
  type MarketplaceFacet,
  type MarketplaceListing,
  type MarketplaceReviews,
  type MarketplaceTagFacet,
} from '@/api/client'
import {
  SutrBadge,
  SutrButton,
  SutrCard,
  SutrDrawer,
  SutrEmpty,
  SutrError,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSearchInput,
  SutrSectionLabel,
  SutrSelect,
  SutrSpinner,
  describeError,
} from '@/components/sutr'

const SORTS: Array<{ value: string; label: string }> = [
  { value: 'popular', label: 'Most installed' },
  { value: 'rating', label: 'Highest rated' },
  { value: 'name', label: 'Name' },
]

const PAGE_SIZE = 24

function Stars({ rating, count }: { rating: number | null; count: number }) {
  if (rating === null) {
    // "Not rated" is not the same as "rated zero", and must not look like it.
    return <span className="sutr-muted">Not rated yet</span>
  }
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      <Star size={12} fill="currentColor" />
      {rating.toFixed(1)}
      <span className="sutr-muted">({count})</span>
    </span>
  )
}

/**
 * The marketplace: the tool catalogue as a storefront rather than a list.
 *
 * Trust score, compliance, regions, pricing and versions are shown as
 * "not available yet" rather than as zeros — they are owned by the Registry
 * service, which is not implemented. Rendering a 0 trust score would tell the
 * reader something false.
 */
export default function MarketplacePage() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()

  const [listings, setListings] = useState<MarketplaceListing[]>([])
  const [total, setTotal] = useState(0)
  const [categories, setCategories] = useState<MarketplaceFacet[]>([])
  const [tags, setTags] = useState<MarketplaceTagFacet[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [query, setQuery] = useState(params.get('q') ?? '')
  const category = params.get('category') ?? ''
  const activeTags = useMemo(() => params.getAll('tag'), [params])
  const sort = params.get('sort') ?? 'popular'
  const offset = Number(params.get('offset') ?? 0)

  const [selected, setSelected] = useState<MarketplaceListing | null>(null)
  const [reviews, setReviews] = useState<MarketplaceReviews | null>(null)
  const [myRating, setMyRating] = useState(0)
  const [reviewBody, setReviewBody] = useState('')
  const [reviewError, setReviewError] = useState<string | null>(null)

  const setParam = useCallback(
    (key: string, value: string | null) => {
      const next = new URLSearchParams(params)
      if (value) next.set(key, value)
      else next.delete(key)
      // Any filter change resets paging: page 3 of a different filter is
      // almost never what the reader wanted.
      if (key !== 'offset') next.delete('offset')
      setParams(next, { replace: true })
    },
    [params, setParams],
  )

  function toggleTag(tag: string) {
    const next = new URLSearchParams(params)
    const current = next.getAll('tag')
    next.delete('tag')
    for (const existing of current) {
      if (existing !== tag) next.append('tag', existing)
    }
    if (!current.includes(tag)) next.append('tag', tag)
    next.delete('offset')
    setParams(next, { replace: true })
  }

  useEffect(() => {
    const handle = setTimeout(() => setParam('q', query || null), 250)
    return () => clearTimeout(handle)
    // `setParam` changes with `params`; depending on it here would re-fire the
    // debounce on every URL change rather than only on typing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.marketplace
      .listings({
        q: params.get('q') ?? undefined,
        category: category || undefined,
        tags: activeTags,
        sort,
        limit: PAGE_SIZE,
        offset,
      })
      .then((page) => {
        if (cancelled) return
        setListings(page.listings)
        setTotal(page.total)
        setError(null)
      })
      .catch((err) => {
        if (!cancelled) setError(describeError(err).message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [params, category, activeTags, sort, offset])

  useEffect(() => {
    void api.marketplace
      .categories()
      .then(setCategories)
      .catch(() => setCategories([]))
    void api.marketplace
      .tags(24)
      .then(setTags)
      .catch(() => setTags([]))
  }, [])

  function open(listing: MarketplaceListing) {
    setSelected(listing)
    setReviews(null)
    setReviewError(null)
    setMyRating(0)
    setReviewBody('')
    void api.marketplace
      .reviews(listing.integration_id)
      .then(setReviews)
      .catch(() => setReviews(null))
  }

  async function submitReview() {
    if (!selected || myRating < 1) return
    try {
      await api.marketplace.review(
        selected.integration_id,
        myRating,
        undefined,
        reviewBody || undefined,
      )
      const [fresh, listing] = await Promise.all([
        api.marketplace.reviews(selected.integration_id),
        api.marketplace.listing(selected.integration_id),
      ])
      setReviews(fresh)
      setSelected(listing)
      setListings((current) =>
        current.map((entry) => (entry.integration_id === listing.integration_id ? listing : entry)),
      )
      setReviewBody('')
      setReviewError(null)
    } catch (err) {
      setReviewError(describeError(err).message)
    }
  }

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Catalogue"
        title="Marketplace"
        subtitle={`${total} tool source${total === 1 ? '' : 's'} available to this organization.`}
      >
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
          <SutrSearchInput
            value={query}
            onValueChange={setQuery}
            ariaLabel="Search the marketplace"
            placeholder="Search by name, description, or tag"
            maxWidth={360}
          />
          <SutrSelect
            value={category}
            aria-label="Filter by category"
            onChange={(event) => setParam('category', event.target.value || null)}
          >
            <option value="">All categories</option>
            {categories.map((facet) => (
              <option key={facet.category} value={facet.category}>
                {facet.category} ({facet.count})
              </option>
            ))}
          </SutrSelect>
          <SutrSelect
            value={sort}
            aria-label="Sort"
            onChange={(event) => setParam('sort', event.target.value)}
          >
            {SORTS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </SutrSelect>
        </div>
      </SutrPageHeader>

      <SutrPageBody>
        {tags.length > 0 ? (
          <>
            <SutrSectionLabel>Tags</SutrSectionLabel>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 20 }}>
              {tags.map((facet) => (
                <button
                  key={facet.tag}
                  type="button"
                  className="sutr-chip"
                  aria-pressed={activeTags.includes(facet.tag)}
                  onClick={() => toggleTag(facet.tag)}
                  style={{
                    border: '1px solid var(--sutr-border, #333)',
                    borderRadius: 999,
                    padding: '2px 10px',
                    fontSize: 12,
                    background: activeTags.includes(facet.tag)
                      ? 'var(--sutr-accent-soft, #2a2a2a)'
                      : 'transparent',
                    cursor: 'pointer',
                  }}
                >
                  <Tag size={10} style={{ marginRight: 4 }} />
                  {facet.tag} <span className="sutr-muted">{facet.count}</span>
                </button>
              ))}
            </div>
          </>
        ) : null}

        {error ? <SutrError what={error} /> : null}
        {loading ? <SutrSpinner /> : null}

        {!loading && listings.length === 0 ? (
          <SutrEmpty
            title="Nothing matches those filters"
            body="Clear the search or pick a different category."
          />
        ) : null}

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
            gap: 12,
          }}
        >
          {listings.map((listing) => (
            <SutrCard key={listing.integration_id}>
              <button
                type="button"
                onClick={() => open(listing)}
                style={{
                  all: 'unset',
                  cursor: 'pointer',
                  display: 'block',
                  padding: 14,
                  width: '100%',
                  boxSizing: 'border-box',
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    gap: 8,
                    alignItems: 'baseline',
                  }}
                >
                  <strong>{listing.name}</strong>
                  {listing.installed ? (
                    <SutrBadge tone="success">
                      <Check size={10} /> Installed
                    </SutrBadge>
                  ) : null}
                </div>
                <div className="sutr-muted" style={{ fontSize: 12, marginTop: 6, minHeight: 32 }}>
                  {listing.description ?? 'No description provided.'}
                </div>
                <div
                  style={{
                    display: 'flex',
                    gap: 10,
                    marginTop: 10,
                    fontSize: 12,
                    alignItems: 'center',
                    flexWrap: 'wrap',
                  }}
                >
                  <SutrBadge tone="neutral">{listing.category}</SutrBadge>
                  <span className="sutr-muted" style={{ display: 'inline-flex', gap: 4 }}>
                    <Download size={12} />
                    {listing.install_count}
                  </span>
                  <Stars rating={listing.rating} count={listing.review_count} />
                </div>
              </button>
            </SutrCard>
          ))}
        </div>

        {total > PAGE_SIZE ? (
          <div style={{ display: 'flex', gap: 8, marginTop: 20, alignItems: 'center' }}>
            <SutrButton
              disabled={offset === 0}
              onClick={() => setParam('offset', String(Math.max(offset - PAGE_SIZE, 0)))}
            >
              Previous
            </SutrButton>
            <span className="sutr-muted" style={{ fontSize: 12 }}>
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
            </span>
            <SutrButton
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setParam('offset', String(offset + PAGE_SIZE))}
            >
              Next
            </SutrButton>
          </div>
        ) : null}
      </SutrPageBody>

      <SutrDrawer
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.name ?? ''}
      >
        {selected ? (
          <div style={{ display: 'grid', gap: 16 }}>
            <p style={{ margin: 0 }}>{selected.description ?? 'No description provided.'}</p>

            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <SutrBadge tone="neutral">{selected.category}</SutrBadge>
              {selected.tags.slice(0, 8).map((tag) => (
                <SutrBadge key={tag} tone="neutral" plain>
                  {tag}
                </SutrBadge>
              ))}
            </div>

            <dl
              style={{
                display: 'grid',
                gridTemplateColumns: 'auto 1fr',
                gap: '6px 16px',
                margin: 0,
              }}
            >
              <dt className="sutr-muted">Type</dt>
              <dd style={{ margin: 0 }}>{selected.type}</dd>
              <dt className="sutr-muted">Installs</dt>
              <dd style={{ margin: 0 }}>{selected.install_count}</dd>
              <dt className="sutr-muted">Tools</dt>
              <dd style={{ margin: 0 }}>
                {selected.tool_count ?? <span className="sutr-muted">Discovered on connect</span>}
              </dd>
              <dt className="sutr-muted">Rating</dt>
              <dd style={{ margin: 0 }}>
                <Stars rating={selected.rating} count={selected.review_count} />
              </dd>
              <dt className="sutr-muted">Auth</dt>
              <dd style={{ margin: 0 }}>{selected.auth_methods.join(', ') || 'none'}</dd>
            </dl>

            {selected.pending_fields.length > 0 ? (
              <SutrCard tone="flush">
                <div style={{ padding: 12, fontSize: 12 }}>
                  <strong>Not available yet:</strong> {selected.pending_fields.join(', ')}.
                  <div className="sutr-muted" style={{ marginTop: 4 }}>
                    {selected.pending_reason}
                  </div>
                </div>
              </SutrCard>
            ) : null}

            {!selected.available && selected.available_reason ? (
              <SutrError what={selected.available_reason} />
            ) : null}

            <div style={{ display: 'flex', gap: 8 }}>
              <SutrButton onClick={() => navigate(`/app/integrations/${selected.integration_id}`)}>
                {selected.installed ? 'Manage' : 'Connect'}
              </SutrButton>
              {selected.docs_url ? (
                <a
                  href={selected.docs_url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="sutr-link"
                >
                  Provider documentation
                </a>
              ) : null}
            </div>

            <div>
              <SutrSectionLabel>Reviews</SutrSectionLabel>
              {reviews && reviews.reviews.length > 0 ? (
                <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'grid', gap: 10 }}>
                  {reviews.reviews.map((review) => (
                    <li key={review.id} style={{ fontSize: 13 }}>
                      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                        <Stars rating={review.rating} count={0} />
                        <span className="sutr-muted">{review.author}</span>
                      </div>
                      {review.body ? <div style={{ marginTop: 2 }}>{review.body}</div> : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="sutr-muted" style={{ fontSize: 13, margin: 0 }}>
                  No reviews from your organization yet.
                </p>
              )}

              <div style={{ marginTop: 12, display: 'grid', gap: 8 }}>
                <div style={{ display: 'flex', gap: 4 }}>
                  {[1, 2, 3, 4, 5].map((score) => (
                    <button
                      key={score}
                      type="button"
                      aria-label={`Rate ${score} out of 5`}
                      onClick={() => setMyRating(score)}
                      style={{
                        all: 'unset',
                        cursor: 'pointer',
                        color: score <= myRating ? 'inherit' : 'var(--sutr-muted, #888)',
                      }}
                    >
                      <Star size={16} fill={score <= myRating ? 'currentColor' : 'none'} />
                    </button>
                  ))}
                </div>
                <textarea
                  className="sutr-input"
                  rows={3}
                  placeholder="What did you use it for? (optional)"
                  value={reviewBody}
                  onChange={(event) => setReviewBody(event.target.value)}
                />
                {reviewError ? <SutrError what={reviewError} /> : null}
                <div>
                  <SutrButton disabled={myRating < 1} onClick={() => void submitReview()}>
                    Save rating
                  </SutrButton>
                </div>
              </div>
            </div>
          </div>
        ) : null}
      </SutrDrawer>
    </SutrPage>
  )
}
