"""Wall-clock allocation. Parallel lanes count once; every provider shares its slot."""
DETERMINISTIC = 300
LANES = 1200  # each lane TOTAL: one author session, transport takeover, seed fetches
BODY = 180
WRITER = 720  # primary + Hermes
HTML = 600    # primary + Hermes + controlled rendering
VALIDATION = 120
FINAL_REVIEW = 300  # reserved for a fresh chairman/finalize turn, outside runner
TOTAL = sum((DETERMINISTIC, LANES, BODY, WRITER, HTML, VALIDATION, FINAL_REVIEW))
RUNNER = TOTAL - FINAL_REVIEW


def allocation(total=TOTAL):
    if total < TOTAL:
        raise ValueError('pipeline total must reserve all stages (%ds)' % TOTAL)
    return dict(deterministic=DETERMINISTIC, lanes=LANES, body=BODY,
                writer=WRITER, html=HTML, validation=VALIDATION,
                final_review=FINAL_REVIEW, total=TOTAL, runner=RUNNER)


if __name__ == '__main__':
    import json
    print(json.dumps(dict(allocation(), minutes=TOTAL / 60, runner_minutes=RUNNER / 60), indent=2))
