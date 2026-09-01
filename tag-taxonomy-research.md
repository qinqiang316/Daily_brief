# Fine-Grained Tag Taxonomy for Content Recommendation System

## Research Summary

### 1. Right Granularity Level

**Industry consensus: 30-50 leaf tags maximum** for a content classification taxonomy.

**Key principles:**
- **Too few tags (<15):** All articles collapse into the same bucket, no differentiation for personalization. With only 6 current categories, you can't distinguish "AI safety researcher" from "AI startup founder."
- **Too many tags (>80):** Sparse data problem — with 10-50 liked articles, most tags will have 0-1 hits. Cold start kills the system.
- **Sweet spot (30-40 leaf tags):** With 10-50 liked articles, each tag averages 0.25-1.5 articles. Manageable with fallback to parent category.
- **3-5 tags per article** is the practical maximum for auto-tagging accuracy.

**Recommended architecture: 2-level hierarchy**
- **Level 1 (8-10 broad domains):** Used for fallback when leaf data is sparse
- **Level 2 (30-35 leaf tags):** Used for fine-grained matching when enough data exists
- **Tag each article with 2-4 leaf tags** (one primary, 1-3 secondary)

**Decision rule for when to use leaf vs. parent:**
```
if tag_hit_count >= 3:
    use leaf_tag for matching
else:
    fall back to parent_domain
```

---

### 2. Auto-Tagging Pipeline (Lightweight, No GPU Required)

**Layer 1: Rule-based keyword matching (fast, deterministic)**
```python
DOMAIN_KEYWORDS = {
    "ai-ml": ["LLM", "transformer", "neural network", "GPT", "diffusion model",
              "reinforcement learning", "fine-tuning", "RLHF", "embedding"],
    "sw-eng": ["Kubernetes", "microservice", "CI/CD", "API", "database",
               "compiler", "debugging", "refactoring", "code review"],
    # ... etc for each domain
}
# Match = fast, no ML needed. Handles 60-70% of articles.
```

**Layer 2: YAKE keyword extraction (no training, unsupervised)**
- YAKE! is the best lightweight option: no corpus, no dictionaries, no training
- Extracts top 10 keywords per article in <100ms
- Map extracted keywords to tag vocabulary via fuzzy matching
- Good for catching articles that don't match rule-based keywords

```python
import yake
kw_extractor = yake.KeywordExtractor(lan="zh", n=2, top=10)
keywords = kw_extractor.extract_keywords(article_text)
# keywords = [("neural architecture search", 0.02), ("transformer", 0.05), ...]
```

**Layer 3: TF-IDF + cosine similarity (corpus-level)**
- Build TF-IDF matrix across all articles in corpus
- Compare each new article against tag "seed documents" (manually written 1-sentence descriptions of each tag)
- Best for: articles that are borderline between two tags

**Layer 4 (optional): Simple NLP via spaCy (lightweight)**
- Named Entity Recognition (NER) for extracting people, orgs, products
- Part-of-speech tagging to find noun phrases as tag candidates
- Dependency parsing to understand "X is better than Y" → stance detection

**Practical pipeline order:**
```
Article → Rule match? → Yes → assign tags
                ↓ No
         YAKE extract keywords → fuzzy match to vocabulary → assign tags
                ↓ No good match
         TF-IDF vs seed docs → assign tags
                ↓ Still low confidence
         Manual review queue / assign parent category only
```

---

### 3. Building "Opposite Direction" Relationships

This is the hardest part. Three approaches, from simplest to most sophisticated:

#### Approach A: Manual antonym pairs (recommended for starting)
```python
OPPOSITIONS = {
    "pro-automation":    "pro-human-labor",
    "bullish-on-AI":     "AI-skeptic",
    "degrowth":          "growth-optimist",
    "centralized":       "decentralized",
    "fast-shipping":     "methodical-dev",
}
```
- User likes "pro-automation" → system can recommend "pro-human-labor" as **contrast** (not just similar)
- Simple, explicit, maintainable

#### Approach B: Stance detection via sentiment keywords
```python
STANCE_SIGNALS = {
    "pro-automation": ["accelerate", "disruption is good", "efficiency gains", "automation dividend"],
    "pro-human-labor": ["job displacement", "workers rights", "human-in-the-loop", "labor exploitation"],
    "bullish-on-AI": ["exponential growth", "AGI imminent", "AI will transform", "unprecedented capability"],
    "AI-skeptic": ["AI bubble", "overhyped", "narrow AI only", "alignment problem", "existential risk"],
}
# Count signal keywords in article → assign stance tag
```

#### Approach C: Embedding-based opposition (advanced)
- Compute article embedding (e.g., via sentence-transformers)
- Compute difference vector between opposing tag centroids
- If article embedding is closer to tag A than tag B, and distance > threshold → assign stance
- Requires ~20+ articles per tag to be reliable

**Recommendation: Start with A + B, graduate to C when data allows.**

#### How oppositions drive recommendations:
```
User likes 3 "bullish-on-AI" articles
→ System knows user's stance on AI
→ Can recommend:
  (a) MORE bullish articles (reinforcement)
  (b) AI-skeptic articles (devil's advocate / serendipity)
  (c) OTHER pro-X articles (cross-domain stance matching)
```

---

## Concrete Tag Hierarchy (35 leaf tags)

```
TOPIC_DOMAIN
├── AI / Machine Learning          (parent)
│   ├── ai-foundations             (transformers, neural nets, training)
│   ├── ai-applications            (code gen, image gen, search)
│   ├── ai-safety-alignment        (alignment, interpretability, risk)
│   └── ai-business                (funding, products, commercialization)
│
├── Software Engineering           (parent)
│   ├── sw-arch-systems            (distributed systems, infra, DevOps)
│   ├── sw-practice                (testing, code quality, team process)
│   └── sw-tools-languages         (new languages, frameworks, editors)
│
├── Hardware / Chips               (parent)
│   ├── hw-chips-design            (GPU, TPU, FPGA, chip architecture)
│   └── hw-manufacturing-supply    (fab, supply chain, geopolitics)
│
├── Business / Startups            (parent)
│   ├── biz-startup                (founding, fundraising, growth)
│   └── biz-strategy               (competitive moats, market analysis)
│
├── Economics / Markets            (parent)
│   ├── econ-macro                  (rates, inflation, cycles)
│   ├── econ-markets                (stocks, crypto, valuations)
│   └── econ-structural             (inequality, degrowth, labor)
│
├── Psychology / Mental Health     (parent)
│   ├── psych-cognitive             (biases, decision-making, habits)
│   └── psych-wellbeing             (burnout, resilience, mindfulness)
│
├── Lifestyle                      (parent)
│   ├── life-productivity           (systems, tools, habits)
│   └── life-philosophy             (minimalism, meaning, values)
│
├── Rail Transit / Maglev          (parent)
│   ├── rail-technology             (maglev, hyperloop, propulsion)
│   └── rail-industry               (projects, policy, investment)
│
└── General Science                (parent)
    ├── sci-physics-materials       (quantum, superconductors, materials)
    └── sci-space-earth             (space, climate, earth science)
```

**Total: 9 parent domains × ~2-4 leaf tags each = 35 leaf tags**

---

## Stance/Attitude Overlay Tags (orthogonal to topic)

These are cross-cutting tags that can combine with any topic tag:

```
STANCE_OVERLAY (pick 0-1 per article)
├── pro-automation          ↔  pro-human-labor
├── bullish-on-AI           ↔  AI-skeptic
├── growth-optimist         ↔  degrowth
├── centralized             ↔  decentralized
├── fast-shipping           ↔  methodical-dev
├── empirical               ↔  theoretical
├── mainstream              ↔  contrarian
└── optimistic              ↔  cautious
```

**8 stance pairs = 16 stance tags** (but articles only get 0-1 of these)

---

## Implementation Recommendations

### Data Cold Start Strategy (10-50 articles)
1. **Tag everything with parent category first** (Level 1 always works)
2. **Only use leaf tags when >=3 articles share that leaf tag**
3. **Stance tags require >=5 articles** before they influence recommendations
4. **Rule-based tagging for 80% of articles, manual review for 20%**

### Tag Vocabulary Management
```python
# Tag registry with metadata
TAG_REGISTRY = {
    "ai-foundations": {
        "parent": "ai-ml",
        "keywords": ["transformer", "attention", "backpropagation", "gradient descent"],
        "min_articles": 3,       # minimum articles before using this tag
        "stance_pair": None,     # no stance pair for this topic
    },
    "bullish-on-AI": {
        "parent": None,          # stance tags don't have parents
        "keywords": ["exponential", "AGI", "transform", "unprecedented"],
        "min_articles": 5,       # stance tags need more data
        "stance_pair": "AI-skeptic",
        "stance_signal": "positive",
    },
    # ...
}
```

### Recommended Tag Count by Corpus Size
| Articles | Leaf Tags Usable | Parent Tags Usable | Stance Tags Usable |
|----------|-----------------|--------------------|--------------------|
| 10-20    | 0               | 9 (all)            | 0                  |
| 20-50    | 5-10 (popular)  | 9 (all)            | 0-2 (most common)  |
| 50-100   | 15-25           | 9 (all)            | 4-6                |
| 100+     | 35 (all)        | 9 (all)            | all 8 pairs        |

### Auto-Tagging Quality Metrics
- **Precision:** % of auto-tags that a human would agree with → target >80%
- **Coverage:** % of articles that get at least 1 tag → target >95%
- **Consistency:** same article tagged same way on re-run → target >90%

---

## Key Insights from Research

1. **YAKE! is the sweet spot** for lightweight keyword extraction: no training, no GPU, supports Chinese, works on any text length. TF-IDF + cosine similarity is good as a second pass but YAKE alone handles most cases.

2. **30-50 is the universal sweet spot** for tag count across taxonomy design literature. The constraint is human cognitive load (if manual tagging) and data sparsity (if auto-tagging). For auto-tagging with sparse data, err toward 30-35.

3. **Opposition/stance detection** is an active research area (stance detection in NLP). For a practical system, start with keyword-based stance signals (Approach B) and only invest in embedding-based approaches (Approach C) once you have 100+ labeled articles.

4. **Hierarchical fallback** is critical: always have parent categories as fallback when leaf tag data is sparse. This prevents the "cold start" problem from killing the entire recommendation quality.
