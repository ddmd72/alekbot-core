# RFC: Deliberate Fact Management System

**Status:** Proposed (Schema Redesign v2.2 - Ready for Implementation)  
**Created:** 2026-02-16  
**Updated:** 2026-02-16 (v2.2: Architecture Finalization + Reviewer Feedback Integration)  
**Author:** AI Development Team  
**Stakeholders:** Architecture, Domain Layer, Application Services, Data Migration Team

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Proposed Solution](#3-proposed-solution)
4. [Domain Model Changes](#4-domain-model-changes)
5. [Tool Specifications](#5-tool-specifications)
6. [Consolidation Agent Prompt](#6-consolidation-agent-prompt)
7. [Hexagonal Architecture](#7-hexagonal-architecture)
8. [Implementation Plan](#8-implementation-plan)
9. [Testing Strategy](#9-testing-strategy)
10. [Risk Assessment](#10-risk-assessment)
11. [Appendices](#11-appendices)

---

## 1. Executive Summary

This RFC proposes a comprehensive overhaul of the Consolidation Agent to transform it from a **fast deduplicator** into a **deliberate fact curator**. The new system introduces explicit fact taxonomy (Domain, Temporal Class, State), awareness-based decision making (search before create), and fine-grained fact management tools.

### Key Changes

- **4-Dimensional Fact Taxonomy** (Domain × Temporal Class × State × Context Priority)
- **Awareness-First Strategy** (search existing facts before deciding)
- **5 New Tools** (search, create, update, merge, discard)
- **Deliberate Cognitive Process** (8 steps with explicit reasoning)
- **Quality over Speed** (60-120s latency acceptable)
- **LLM-Driven Classification** (cognitive framework, not hardcoded rules)

### Impact

✅ **Eliminates duplicate facts** (currently: "User is integrating Gemini" saved 10+ times)  
✅ **Reduces noise** from ephemeral states (auto-archives after 14 days)  
✅ **Enables intelligent updates** (weight time series, not separate entries)  
✅ **Improves retrieval precision** (taxonomy-based filtering)

### Metrics

| Metric                   | Before     | After (Target)      |
| ------------------------ | ---------- | ------------------- |
| Duplicate fact rate      | 15-20%     | <2%                 |
| Ephemeral fact pollution | ~40% of DB | <5% (auto-archived) |
| Consolidation latency    | 5-10s      | 60-120s             |
| Fact quality score       | 6.5/10     | 9/10                |
| Update vs Create ratio   | 0:100      | 30:70               |

---

## 2. Problem Statement

### 2.1 Current Issues

#### Problem 1: Uncontrolled Tag Generation

**Symptom:** LLM invents new tags each time without predefined taxonomy.

**Example:**

```python
# Same concept, different tags:
["gemini", "ai", "integration"]
["gemini-api", "genai", "workflow"]
["gemini_integration", "google-ai", "evaluation"]
```

**Impact:** Semantic search unreliable, retrieval precision drops by 40%.

---

#### Problem 2: Ephemeral Noise Pollution

**Symptom:** Temporary work states treated as permanent biographical facts.

**Example:**

```python
# All saved as separate permanent facts:
"User is actively integrating Google Gemini (GenAI) into their workflow"
"User is evaluating Vertex AI Grounding versus File API"
"User is implementing Gmail automation system"
"User is developing a software project utilizing hexagonal architecture"
```

**Impact:**

- 40% of fact database is ephemeral noise
- Biographical context polluted with stale project states
- Retrieval includes irrelevant "current work" from 6 months ago

---

#### Problem 3: Duplicate Facts

**Symptom:** Same information stored multiple times with slight variations.

**Example:**

```python
# 3 separate facts for same information:
"User's current weight is 81 kg"
"User weighs 81kg as of Feb 7"
"User's weight: 81 kg (February 2026)"
```

**Root Cause:**

- No awareness of existing facts before creation
- No UPDATE mechanism (only CREATE)
- No deduplication beyond semantic similarity check

**Impact:** 15-20% of facts are duplicates, wasted storage and embedding costs.

---

#### Problem 4: Lack of Temporal Context

**Symptom:** Facts stored without lifecycle management or expiration.

**Example:**

```python
"User's current weight is 81 kg"  # When? Still current after 6 months?
"User is working on Gmail API"    # Completed? Still active? Abandoned?
```

**Impact:**

- Cannot distinguish current vs historical facts
- No automatic archival of stale information
- Retrieval includes outdated facts

---

#### Problem 5: Low Consolidation Quality

**Symptom:** Agent records user questions as facts.

**Example:**

```python
User: "what brand is my car?"
Recorded fact: "User is asking about car brand"  # ❌ Not a biographical fact!
```

**Impact:** Database cluttered with meta-information instead of actual facts.

---

#### Problem 6: No Enrichment Strategy

**Symptom:** Agent creates new facts even when UPDATE would be better.

**Example:**

```python
# Existing fact:
"User owns 2010 Toyota Corolla"

# User says: "My car has tinted windows"

# Current behavior (BAD):
Creates: "User's car has tinted windows"  # Separate fact

# Desired behavior (GOOD):
Updates existing: "User owns 2010 Toyota Corolla with tinted windows"
```

**Impact:** Fragmented information, poor retrieval relevance.

---

### 2.2 Current Architecture Limitations

**Current Flow:**

```
Conversation → Consolidation Agent → LLM (extract facts) → Semantic Dedup → Save All
```

**Limitations:**

1. **No Firestore awareness** - Agent doesn't query existing facts
2. **No decision logic** - Always CREATE, never UPDATE/MERGE
3. **No taxonomy enforcement** - LLM free-forms tags and structure
4. **No lifecycle** - Facts live forever (no TTL, no archival)

---

## 3. Proposed Solution

### 3.1 Core Principles

**1. Deliberate Over Fast**

- Current: 5-10s latency, low quality
- Target: 60-120s latency, high quality
- Rationale: Biographical memory is append-only, quality matters more than speed

**2. Awareness Before Action**

- Current: Blind CREATE
- Target: SEARCH → ANALYZE → DECIDE (UPDATE vs CREATE vs MERGE vs DISCARD)

**3. Explicit Taxonomy**

- Current: Free-form tags
- Target: Predefined taxonomy (Domain, Temporal Class, State)

**4. Lifecycle Management**

- Current: Facts live forever
- Target: TTL-based archival, state transitions

---

### 3.2 Four-Dimensional Taxonomy

#### Axis 1: Domain (Semantic Category)

**Purpose:** Structural taxonomy for efficient querying and filtering.

**Design Principle:** Balance between coverage and LLM disambiguation. 15 domains cover 95% of biographical facts without overwhelming classification complexity.

| Domain              | Description                            | Examples                                        | Query Use Case                 |
| ------------------- | -------------------------------------- | ----------------------------------------------- | ------------------------------ |
| **BIOGRAPHICAL**    | Immutable identity traits              | birthdate, blood type, citizenship, origin      | "Tell me about myself"         |
| **POSSESSION**      | Owned physical objects                 | car, house, furniture, clothing, gadgets        | "What do I own?"               |
| **HEALTH**          | Medical conditions, biometrics         | weight, chronic conditions, allergies, symptoms | "My health history"            |
| **MEDICAL_RECORDS** | Clinical data, test results, diagnoses | lab results, x-rays, prescriptions, surgeries   | "Find my blood test from 2025" |
| **LOCATION**        | Addresses, residence, travel           | home address, office, city, country, trips      | "Where have I lived?"          |
| **WORK**            | Occupation, career, employment         | job title, company, salary, methodologies       | "My work experience"           |
| **NETWORK**         | Contacts, relationships, social        | family, friends, colleagues, mentors            | "Who are my contacts?"         |
| **PREFERENCE**      | Habits, likes, dislikes, anchors       | food preferences, values, principles, routines  | "What do I like?"              |
| **SKILL**           | Abilities, knowledge, languages        | programming, languages, certifications, tools   | "What can I do?"               |
| **PROJECT**         | Active work, temporary endeavors       | current projects, evaluations, experiments      | "What am I working on?"        |
| **FINANCE**         | Money, income, expenses, investments   | salary, savings, debts, portfolios, budgets     | "My financial situation"       |
| **EDUCATION**       | Learning, degrees, courses             | university, certifications, training, reading   | "What did I study?"            |
| **LEGAL**           | Legal matters, contracts, rights       | contracts, agreements, licenses, legal issues   | "My legal documents"           |
| **ENTERTAINMENT**   | Leisure, hobbies, media consumption    | books, movies, games, sports, music             | "What do I enjoy?"             |
| **COMMUNICATION**   | Contact info, social media, messaging  | phone, email, social accounts, handles          | "How to reach me?"             |

**Rationale:**

✅ **HEALTH vs MEDICAL_RECORDS** - Health = ongoing state (weight, symptoms). Medical = clinical data (lab results, diagnoses). Separation critical for HIPAA-style precision.

✅ **LEGAL added** - Contracts, agreements, licenses are distinct from WORK and FINANCE.

✅ **FINANCE added** - Money matters deserve separate domain (not merged with WORK or PREFERENCE).

✅ **EDUCATION added** - Learning history is distinct from SKILL (outcome) and WORK (application).

✅ **ENTERTAINMENT + COMMUNICATION** - Cover leisure and connectivity (common user queries).

❌ **No TRANSPORT domain** - Merged into POSSESSION (vehicles) and LOCATION (travel).

❌ **No FOOD domain** - Merged into PREFERENCE (tastes) and HEALTH (diet restrictions).

❌ **No RELATIONSHIP domain** - Merged into NETWORK (people are in network, relationships are metadata).

**Total: 15 domains** - Sweet spot for LLM classification without confusion.

---

#### Axis 2: Temporal Class (Lifecycle)

4 temporal classes with automatic TTL:

| Class         | Description             | TTL              | Examples                                      |
| ------------- | ----------------------- | ---------------- | --------------------------------------------- |
| **PERMANENT** | Cannot change by nature | None             | birthdate, blood type, origin                 |
| **STABLE**    | Rarely changes          | None (versioned) | address, occupation, car ownership            |
| **DYNAMIC**   | Changes regularly       | 90 days          | weight, active projects, reading list         |
| **EPHEMERAL** | Temporary state         | 14 days          | "evaluating Vertex AI", "debugging Gmail API" |

**Lifecycle Transitions:**

```
CURRENT → (30 days no updates) → STALE
STALE → (60 days no updates) → ARCHIVED
CURRENT → (new version) → SUPERSEDED
```

---

#### Axis 3: State (Actuality)

5 states for lifecycle tracking:

| State           | Description                | Transition Rule                  |
| --------------- | -------------------------- | -------------------------------- |
| **CURRENT**     | Active, used now           | Default for new facts            |
| **STALE**       | Outdated, grace period     | After TTL expiry (first warning) |
| **ARCHIVED**    | Cold storage               | After extended inactivity        |
| **SUPERSEDED**  | Replaced by newer version  | When versioned fact updated      |
| **INVALIDATED** | Correction, no longer true | Manual flag or user correction   |

---

#### Axis 4: Context Priority (Baseline Importance)

**Purpose:** Determines which facts belong in static biographical baseline context (injected in every LLM prompt) vs retrieved dynamically via semantic search.

**Why This Matters:**

- **Biographical Baseline** (50 facts): Always included in every conversation → must contain user's name, gender, core identity
- **Semantic Search** (50 facts): Retrieved on-demand based on conversation topic → health data, project details, specific queries

5 priority levels:

| Priority     | Description                    | Baseline Inclusion | Examples                                     |
| ------------ | ------------------------------ | ------------------ | -------------------------------------------- |
| **CRITICAL** | Essential for ANY conversation | Always (100%)      | Name, gender, primary language, core anchors |
| **HIGH**     | Needed for MOST conversations  | Usually (80%)      | Current job, location, key skills, family    |
| **MEDIUM**   | Useful for SOME conversations  | Sometimes (30%)    | Possessions, hobbies, health metrics         |
| **LOW**      | Query-specific only            | Rarely (5%)        | Temporary projects, detailed preferences     |
| **ARCHIVAL** | Historical, no longer relevant | Never (0%)         | Superseded facts, old states                 |

**Classification Principle:**

> "Would EVERY agent need this fact to communicate naturally with the user, regardless of conversation topic?"

- If YES → CRITICAL or HIGH
- If SOMETIMES → MEDIUM
- If ONLY FOR SPECIFIC QUERIES → LOW
- If OBSOLETE → ARCHIVAL

**Examples:**

- "User's name: Dmytro" → CRITICAL (affects addressing, personalization in every message)
- "User's gender: Male" → CRITICAL (affects pronouns, affects UI/UX in every interaction)
- "User works as Senior SE" → HIGH (provides context for most conversations about work, time, availability)
- "User's current weight: 80.5kg" → MEDIUM (relevant for health queries, not for general chat)
- "User debugging Gmail API" → LOW (temporary work, only relevant when discussing this specific project)
- "User's old weight: 82kg (superseded)" → ARCHIVAL (historical data, replaced by newer)

**LLM Classification Framework:**

ConsolidationAgent will classify ContextPriority using cognitive tests (not hardcoded examples):

```
CRITICAL test: "Would EVERY agent need this to ADDRESS user correctly?"
HIGH test: "Would MOST conversations benefit from this context?"
MEDIUM test: "Would SOME conversations need this?"
LOW test: "Only SPECIFIC queries need this?"
ARCHIVAL test: "Is this OBSOLETE or SUPERSEDED?"
```

---

### 3.3 Deliberate Cognitive Process

**New 8-Step Process:**

```
1. EXTRACT: Parse conversation → candidate facts
2. CLASSIFY: Assign Domain, Temporal, State to each candidate
3. SEARCH: Query existing facts (semantic + keyword)
4. ANALYZE: Compare candidate vs existing facts
5. DECIDE: UPDATE existing / CREATE new / MERGE / DISCARD
6. EXECUTE: Call fact_management tools with fact_id
7. VERIFY: Confirm operation success
8. REPORT: Summarize actions taken
```

**Key Difference from Current:**

- **Step 3 (SEARCH)** - NEW: Query Firestore before deciding
- **Step 4 (ANALYZE)** - NEW: Compare candidate to existing
- **Step 5 (DECIDE)** - NEW: Explicit decision logic (not just CREATE)
- **Step 6 (EXECUTE)** - NEW: Fine-grained operations with fact_id

---

### 3.4 Decision Heuristics

#### UPDATE vs CREATE

**UPDATE when:**

- Candidate adds data points to time series (e.g., new weight measurement)
- Candidate provides missing details to existing fact
- Candidate corrects existing fact (mark old as SUPERSEDED)

**CREATE when:**

- Candidate is orthogonal (different aspect of same domain)
- Candidate is new entity (new possession, new contact)
- Candidate is different time period (travel history)

---

#### MERGE Conditions

**MERGE when:**

- Multiple facts describe same entity (car details scattered)
- Facts are complementary (can combine without contradiction)
- Combined fact is more useful than separate facts

**Example:**

```python
# Existing facts:
"User owns 2010 Toyota Corolla"
"Toyota Corolla has automatic gearbox"
"Car is in Example City"

# MERGE →
"User owns 2010 Toyota Corolla with automatic gearbox, based in Example City"
```

---

#### DISCARD Conditions

**DISCARD when:**

- Candidate is EXACT duplicate
- Candidate is too vague ("User is interested in AI")
- Candidate is ephemeral question, not a fact
- Candidate adds zero new information

---

## 4. Domain Model Changes

### 4.1 Current Schema Analysis

**Existing FactEntity (src/domain/entities.py):**

```python
class FactType(str, Enum):
    STATE = "state"         # ❌ Mixed concept: lifecycle + category
    EVENT = "event"         # ❌ Mixed concept: lifecycle + category
    PRINCIPLE = "principle" # ❌ Mixed concept: lifecycle + category
    SYSTEM = "system"       # ✅ System-level (not biographical)
    ALERT = "alert"         # ❌ Mixed concept: lifecycle + category

class FactEntity(BaseModel):
    # Identity
    id: str
    account_id: str  # Multi-tenant support
    created_by_user_id: str  # Creator
    lineage_id: str  # SCD Type 2 versioning

    # Content
    text: str
    vector: Optional[List[float]]  # Text embedding
    tags_vector: Optional[List[float]]  # Multi-vector search
    metadata_vector: Optional[List[float]]  # Multi-vector search
    tags: List[str]  # Free-form keywords
    type: FactType  # ← PROBLEMATIC: mixes lifecycle + category
    metadata: Dict[str, Any]

    # SCD Type 2 (versioning)
    created_at: datetime
    valid_from: datetime
    valid_to: Optional[datetime]
    is_current: bool  # Latest version in lineage

    # Access control
    visibility: FactVisibility  # ACCOUNT_SHARED | USER_PRIVATE
```

**Problems with Current Schema:**

1. ❌ **FactType violates SRP** - mixes lifecycle (PERMANENT/DYNAMIC) with category (what domain)
2. ❌ **No structural taxonomy** - tags are free-form, order-dependent
3. ❌ **No explicit TTL** - lifecycle implicit in FactType
4. ❌ **No actuality tracking** - is_current tracks version, not business state
5. ❌ **No version number** - only lineage_id + is_current

---

### 4.2 New Clean Architecture Schema

**Updated FactEntity (AFTER migration):**

```python
from enum import Enum
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from uuid import uuid4
from pydantic import BaseModel, Field

# ========================================================================
# NEW: 3D Taxonomy Enums (Clean Architecture)
# ========================================================================

class FactDomain(str, Enum):
    """WHAT is this fact about? (Structural category)"""
    BIOGRAPHICAL = "biographical"    # Immutable identity
    POSSESSION = "possession"        # Owned objects
    HEALTH = "health"                # Medical conditions, biometrics
    MEDICAL_RECORDS = "medical_records"  # Clinical data, diagnoses
    LOCATION = "location"            # Addresses, residence
    WORK = "work"                    # Occupation, career
    NETWORK = "network"              # Contacts, relationships
    PREFERENCE = "preference"        # Habits, likes, anchors
    SKILL = "skill"                  # Abilities, languages
    PROJECT = "project"              # Active work (usually EPHEMERAL)
    FINANCE = "finance"              # Money, investments
    EDUCATION = "education"          # Learning, degrees
    LEGAL = "legal"                  # Contracts, licenses
    ENTERTAINMENT = "entertainment"  # Hobbies, media
    COMMUNICATION = "communication"  # Contact info, social media

class TemporalClass(str, Enum):
    """HOW LONG does this fact live? (Lifecycle category)"""
    PERMANENT = "permanent"  # Never changes (birthdate, blood type)
    STABLE = "stable"        # Rarely changes (address, job, car)
    DYNAMIC = "dynamic"      # Changes regularly (weight, projects)
    EPHEMERAL = "ephemeral"  # Temporary state (debugging, evaluating)

class FactState(str, Enum):
    """IS this fact current/stale/archived? (Actuality status)"""
    CURRENT = "current"        # Active, used now
    STALE = "stale"            # Not updated recently (grace period)
    ARCHIVED = "archived"      # Cold storage (not shown by default)
    SUPERSEDED = "superseded"  # Replaced by newer version
    INVALIDATED = "invalidated"# Correction, no longer true

class ContextPriority(str, Enum):
    """HOW IMPORTANT is this fact for baseline context? (Baseline inclusion)"""
    CRITICAL = "critical"  # Always in baseline (name, gender, core anchors)
    HIGH = "high"          # Usually in baseline (job, location, skills)
    MEDIUM = "medium"      # Sometimes in baseline (possessions, hobbies)
    LOW = "low"            # Query-specific only (temp projects, details)
    ARCHIVAL = "archival"  # Never in baseline (superseded, historical)

class FactVisibility(str, Enum):
    """Access control (Multi-tenant OAuth)"""
    ACCOUNT_SHARED = "account_shared"  # Visible to all account members
    USER_PRIVATE = "user_private"      # Visible only to creator

# ========================================================================
# UPDATED FactEntity (Clean 4D Architecture)
# ========================================================================

class FactEntity(BaseModel):
    # --- Identity (unchanged) ---
    id: str = Field(default_factory=lambda: str(uuid4()))
    account_id: str  # Billing account owner
    created_by_user_id: str  # User who created fact
    lineage_id: str  # Links all versions (SCD Type 2)

    # --- Content (unchanged) ---
    text: str
    vector: Optional[List[float]] = None  # Text embedding
    tags_vector: Optional[List[float]] = None  # Multi-vector search
    metadata_vector: Optional[List[float]] = None  # Multi-vector search
    tags: List[str] = []  # Free-form semantic keywords
    metadata: Dict[str, Any] = {}

    # ========================================================================
    # NEW: 4D Taxonomy (Explicit Separation of Concerns)
    # ========================================================================
    domain: FactDomain  # WHAT (structural category)
    temporal_class: TemporalClass  # WHEN (lifecycle)
    state: FactState = FactState.CURRENT  # STATUS (actuality)
    context_priority: ContextPriority = ContextPriority.MEDIUM  # IMPORTANCE (baseline inclusion)

    # ========================================================================
    # NEW: Explicit Lifecycle Management
    # ========================================================================
    ttl_days: Optional[int] = None  # Explicit TTL (overrides default)
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None  # Calculated: last_updated + ttl_days

    # ========================================================================
    # NEW: Explicit Version Tracking
    # ========================================================================
    version: int = 1  # Incremented on UPDATE
    replaces_fact_id: Optional[str] = None  # For SUPERSEDED facts

    # ========================================================================
    # NEW: Context
    # ========================================================================
    context: Optional[str] = None  # "Q1 2026 project", "January weight loss"
    reported_date: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # --- SCD Type 2 (unchanged, technical versioning) ---
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_from: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_to: Optional[datetime] = None
    is_current: bool = True  # Latest version in lineage

    # --- Access Control (unchanged) ---
    visibility: FactVisibility = FactVisibility.ACCOUNT_SHARED

    # ========================================================================
    # REMOVED: FactType (replaced by temporal_class)
    # ========================================================================
    # type: FactType  # ❌ Removed - was mixing category + lifecycle
```

---

### 4.3 Schema Comparison (Before vs After)

| Aspect             | BEFORE (FactType)                          | AFTER (3D Taxonomy)                                   |
| ------------------ | ------------------------------------------ | ----------------------------------------------------- |
| **Category**       | ❌ Implicit in tags (free-form)            | ✅ Explicit domain field (15 fixed values)            |
| **Lifecycle**      | ❌ Implicit in FactType (5 mixed values)   | ✅ Explicit temporal_class (4 lifecycle categories)   |
| **Actuality**      | ❌ is_current (technical version tracking) | ✅ state field (5 business states)                    |
| **TTL**            | ❌ Hardcoded per FactType                  | ✅ Explicit ttl_days (can override)                   |
| **Version**        | ❌ Implicit (count lineage)                | ✅ Explicit version number                            |
| **Orthogonality**  | ❌ FactType mixes 2 concepts               | ✅ 3 independent axes (Domain ⊥ Temporal ⊥ State)     |
| **Query**          | ❌ WHERE type="STATE" (ambiguous)          | ✅ WHERE domain="HEALTH" AND temporal_class="DYNAMIC" |
| **Extensibility**  | ❌ Add FactType = code change              | ✅ Add domain = enum extension only                   |
| **Classification** | ❌ LLM guesses from tags                   | ✅ LLM selects from fixed 15 domains                  |

---

### 4.4 Default TTL by Temporal Class

```python
# Default TTL mapping (can be overridden per-fact):

DEFAULT_TTL = {
    TemporalClass.PERMANENT: None,      # Never expires
    TemporalClass.STABLE: None,         # Versioned, not expired
    TemporalClass.DYNAMIC: 90,          # 90 days
    TemporalClass.EPHEMERAL: 14,        # 14 days
}

# Examples:

# Blood type (PERMANENT):
ttl_days = None  # Never expires

# Address (STABLE):
ttl_days = None  # Versioned via SCD Type 2, not expired

# Weight (DYNAMIC):
ttl_days = 90  # Auto-archived after 90 days of no updates

# Active debugging (EPHEMERAL):
ttl_days = 14  # Auto-archived after 14 days

# Weight tracking (DYNAMIC with override):
ttl_days = 30  # Override: frequent tracking needs shorter TTL
```

---

### 4.5 State Transition Rules

```python
# Automatic state transitions (ConsolidationAgent + LifecycleService):

# 1. CURRENT → STALE (not updated in 30 days)
if fact.state == FactState.CURRENT:
    if (now - fact.last_updated).days > 30:
        fact.state = FactState.STALE

# 2. STALE → ARCHIVED (not updated in 90 days total)
if fact.state == FactState.STALE:
    if (now - fact.last_updated).days > 90:
        fact.state = FactState.ARCHIVED

# 3. CURRENT → SUPERSEDED (new version created)
if new_version_created:
    old_fact.state = FactState.SUPERSEDED
    old_fact.is_current = False  # SCD Type 2 technical flag
    old_fact.valid_to = now

    new_fact.version = old_fact.version + 1
    new_fact.replaces_fact_id = old_fact.id

# 4. Manual INVALIDATED (user correction)
if user_corrects:
    old_fact.state = FactState.INVALIDATED
    old_fact.is_current = False
```

---

### 4.6 Migration Strategy (FactType → 3D Taxonomy)

**Phase 1: Inference Rules**

```python
# Map FactType → TemporalClass (1:1 mapping):

FACTTYPE_TO_TEMPORAL = {
    FactType.EVENT: TemporalClass.PERMANENT,     # Historical events never change
    FactType.PRINCIPLE: TemporalClass.STABLE,    # Anchors rarely change
    FactType.STATE: TemporalClass.DYNAMIC,       # States change regularly
    FactType.ALERT: TemporalClass.EPHEMERAL,     # Alerts are temporary
    FactType.SYSTEM: TemporalClass.PERMANENT,    # System prompts permanent
}

# Infer Domain from tags (heuristic):

def infer_domain_from_tags(tags: List[str]) -> FactDomain:
    """Best-effort domain classification based on keywords."""

    # Health keywords
    if any(kw in tags for kw in ["health", "weight", "medical", "symptom", "condition"]):
        return FactDomain.HEALTH

    # Medical records (clinical data)
    if any(kw in tags for kw in ["lab", "test", "diagnosis", "prescription", "surgery"]):
        return FactDomain.MEDICAL_RECORDS

    # Possession
    if any(kw in tags for kw in ["car", "house", "vehicle", "property", "furniture"]):
        return FactDomain.POSSESSION

    # Work
    if any(kw in tags for kw in ["job", "work", "company", "salary", "career", "occupation"]):
        return FactDomain.WORK

    # Finance
    if any(kw in tags for kw in ["money", "finance", "investment", "salary", "expense", "budget"]):
        return FactDomain.FINANCE

    # Legal
    if any(kw in tags for kw in ["contract", "legal", "license", "agreement", "rights"]):
        return FactDomain.LEGAL

    # Education
    if any(kw in tags for kw in ["study", "education", "university", "course", "degree", "learning"]):
        return FactDomain.EDUCATION

    # Skill
    if any(kw in tags for kw in ["skill", "ability", "language", "programming", "expertise"]):
        return FactDomain.SKILL

    # Network
    if any(kw in tags for kw in ["friend", "family", "contact", "colleague", "relationship"]):
        return FactDomain.NETWORK

    # Location
    if any(kw in tags for kw in ["address", "location", "city", "country", "travel", "home"]):
        return FactDomain.LOCATION

    # Entertainment
    if any(kw in tags for kw in ["book", "movie", "music", "hobby", "game", "sport"]):
        return FactDomain.ENTERTAINMENT

    # Communication
    if any(kw in tags for kw in ["phone", "email", "social", "contact", "telegram", "whatsapp"]):
        return FactDomain.COMMUNICATION

    # Project
    if any(kw in tags for kw in ["project", "integration", "development", "debugging"]):
        return FactDomain.PROJECT

    # Preference (anchors, habits)
    if any(kw in tags for kw in ["preference", "like", "dislike", "anchor", "principle", "habit"]):
        return FactDomain.PREFERENCE

    # Biographical (identity)
    if any(kw in tags for kw in ["birthdate", "origin", "citizenship", "identity", "blood"]):
        return FactDomain.BIOGRAPHICAL

    # Default fallback
    return FactDomain.PREFERENCE  # Conservative default

# Derive State from SCD Type 2 fields:

def derive_state(fact: FactEntity) -> FactState:
    """Derive business state from technical SCD Type 2 fields."""

    now = datetime.now(timezone.utc)

    if fact.is_current:
        # Latest version - check if data is stale
        if fact.valid_from and (now - fact.valid_from).days > 90:
            return FactState.STALE  # Old data, not updated
        return FactState.CURRENT

    elif fact.valid_to and fact.valid_to < now:
        # Expired by TTL
        return FactState.ARCHIVED

    else:
        # Replaced by newer version
        return FactState.SUPERSEDED
```

**Phase 2: Migration Script**

```python
# scripts/migration/migrate_facts_to_v3_taxonomy.py

async def migrate_fact(old_fact: FactEntity) -> FactEntity:
    """Transform old schema → new 3D taxonomy schema."""

    # 1. Map FactType → TemporalClass
    temporal_class = FACTTYPE_TO_TEMPORAL.get(
        old_fact.type,
        TemporalClass.STABLE  # Conservative default
    )

    # 2. Infer Domain from tags
    domain = infer_domain_from_tags(old_fact.tags)

    # 3. Derive State from SCD Type 2 fields
    state = derive_state(old_fact)

    # 4. Calculate TTL
    ttl_days = DEFAULT_TTL[temporal_class]

    if ttl_days:
        expires_at = old_fact.valid_from + timedelta(days=ttl_days)
    else:
        expires_at = None

    # 5. Calculate version (count versions in lineage)
    version = await count_versions_in_lineage(old_fact.lineage_id)

    # 6. Find replaced fact (if SUPERSEDED)
    replaces_fact_id = None
    if state == FactState.SUPERSEDED:
        replaces_fact_id = await find_previous_version(old_fact.lineage_id, old_fact.id)

    # 7. Create updated fact (preserve all old fields)
    new_fact = FactEntity(
        # Copy all existing fields
        **old_fact.dict(exclude={"type"}),  # Exclude old FactType

        # Add new 3D taxonomy fields
        domain=domain,
        temporal_class=temporal_class,
        state=state,
        ttl_days=ttl_days,
        last_updated=old_fact.created_at,  # Initialize from created_at
        expires_at=expires_at,
        version=version,
        replaces_fact_id=replaces_fact_id,
        context=None,  # No context in old facts
        reported_date=old_fact.created_at,
    )

    return new_fact

# Migration execution:

async def run_migration():
    """Migrate all existing facts to new schema."""

    facts_collection = db.collection("dev_facts")

    # Batch processing (1000 facts at a time)
    batch_size = 1000
    offset = 0
    total_migrated = 0
    errors = []

    while True:
        # Fetch batch
        batch = await facts_collection.limit(batch_size).offset(offset).get()

        if not batch:
            break

        # Migrate each fact
        for doc in batch:
            try:
                old_fact = FactEntity(**doc.to_dict())
                new_fact = await migrate_fact(old_fact)

                # Update Firestore
                await facts_collection.document(doc.id).set(new_fact.dict())

                total_migrated += 1

            except Exception as e:
                errors.append({"fact_id": doc.id, "error": str(e)})

        offset += batch_size

        # Progress log
        logger.info(f"Migrated {total_migrated} facts...")

    # Report
    logger.info(f"✅ Migration complete: {total_migrated} facts migrated")

    if errors:
        logger.error(f"❌ {len(errors)} errors occurred")
        for err in errors:
            logger.error(f"  - {err['fact_id']}: {err['error']}")
```

---

### 4.7 Backward Compatibility Guarantees

**Zero Breaking Changes:**

1. ✅ All existing fields preserved
2. ✅ New fields optional with defaults
3. ✅ Migration idempotent (can run multiple times)
4. ✅ Old facts work with new code (graceful degradation)
5. ✅ Rollback possible (old facts keep `type` field during transition)

**Transition Period:**

```python
# During migration (both schemas coexist):

if fact.domain is None:
    # Old fact, infer taxonomy on-the-fly
    fact.domain = infer_domain_from_tags(fact.tags)
    fact.temporal_class = FACTTYPE_TO_TEMPORAL[fact.type]
    fact.state = derive_state(fact)

# After migration complete:
# - Remove inference logic
# - Make domain/temporal_class/state required fields
```

---

## 5. Tool Specifications

### 5.1 Tool 1: search_existing_facts

**Purpose:** Search Firestore for existing facts before making decisions.

**Signature:**

```python
async def search_existing_facts(
    query: str,
    domain: Optional[FactDomain] = None,
    limit: int = 10
) -> List[Dict[str, Any]]
```

**Parameters:**

- `query` (str) - Semantic search query
- `domain` (FactDomain, optional) - Filter by domain
- `limit` (int) - Max results (default: 10)

**Returns:**

```python
[
    {
        "fact_id": "uuid-123",
        "content": "User's current weight is 81 kg",
        "domain": "HEALTH",
        "temporal": "DYNAMIC",
        "state": "CURRENT",
        "tags": ["weight", "health", "biometrics"],
        "reported_date": "2026-02-07",
        "similarity": 0.89  # Semantic similarity score
    },
    ...
]
```

**Implementation (Hexagonal):**

**Port (Domain):**

```python
# src/ports/fact_management_port.py

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class FactManagementPort(ABC):

    @abstractmethod
    async def search_existing_facts(
        self,
        query: str,
        domain: Optional[str] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Search existing facts using semantic similarity."""
        pass
```

**Adapter (Infrastructure):**

```python
# src/adapters/firestore_fact_management_adapter.py

class FirestoreFactManagementAdapter(FactManagementPort):

    async def search_existing_facts(
        self,
        query: str,
        domain: Optional[str] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        # 1. Generate embedding for query
        query_embedding = await self.embedding_service.generate_embedding(query)

        # 2. Build Firestore query
        collection_ref = self.db.collection(self.facts_collection)

        query_ref = collection_ref.where("state", "==", "CURRENT")

        if domain:
            query_ref = query_ref.where("domain", "==", domain)

        # 3. Vector search (HNSW index)
        results = query_ref.find_nearest(
            vector_field="embedding_text",
            query_vector=query_embedding,
            distance_measure="COSINE",
            limit=limit
        ).get()

        # 4. Format results
        facts = []
        for doc in results:
            data = doc.to_dict()
            facts.append({
                "fact_id": doc.id,
                "content": data.get("text"),
                "domain": data.get("domain"),
                "temporal": data.get("temporal_class"),
                "state": data.get("state"),
                "tags": data.get("tags", []),
                "reported_date": data.get("reported_date"),
                "similarity": doc.distance  # Firestore provides this
            })

        return facts
```

---

### 5.2 Tool 2: create_fact

**Purpose:** Create NEW fact when candidate is orthogonal or new entity.

**Signature:**

```python
async def create_fact(
    content: str,
    metadata: Dict[str, Any]
) -> Dict[str, Any]
```

**Parameters:**

- `content` (str) - Fact text
- `metadata` (dict) - Must include:
  - `domain` (FactDomain)
  - `temporal_class` (TemporalClass)
  - `state` (FactState, default: CURRENT)
  - `ttl_days` (int, optional)
  - `tags` (List[str])
  - `context` (str, optional)
  - `reported_date` (datetime)

**Returns:**

```python
{
    "fact_id": "new-uuid-456",
    "status": "created",
    "message": "Fact created successfully"
}
```

**Example:**

```python
result = await create_fact(
    content="User started morning yoga practice (30 min daily)",
    metadata={
        "domain": "HEALTH",
        "temporal_class": "DYNAMIC",
        "state": "CURRENT",
        "ttl_days": 90,
        "tags": ["yoga", "habit", "health", "morning"],
        "context": "Q1 2026 habit formation",
        "reported_date": datetime.now()
    }
)
```

---

### 5.3 Tool 3: update_fact

**Purpose:** Update EXISTING fact (enrichment or revision).

**Signature:**

```python
async def update_fact(
    fact_id: str,
    updates: Dict[str, Any]
) -> Dict[str, Any]
```

**Parameters:**

- `fact_id` (str) - UUID of fact to update
- `updates` (dict) - Fields to update:
  - `content` (str, optional) - New/enriched text
  - `tags` (List[str], optional) - Add tags
  - `temporal_class` (TemporalClass, optional) - Can upgrade/downgrade
  - `state` (FactState, optional)
  - `reported_date` (datetime) - Always update to now

**Returns:**

```python
{
    "fact_id": "uuid-123",
    "status": "updated",
    "version": 2,  # Incremented
    "message": "Fact updated successfully"
}
```

**Update Modes:**

This tool supports **two update modes** depending on the use case:

**Mode 1: In-Place Update (for Time Series & Enrichment)**

- Used for: Weight tracking, financial data, skill progress, adding details
- Behavior: Modifies `content` field directly on the same document
- Version: Increments `version` counter
- SCD Type 2: Does NOT create new document, `is_current` remains `True`
- Example: "User's weight: 80.5 kg (Feb 16), was 81 kg (Feb 7)" → appends to same fact

**Mode 2: SCD Type 2 Correction (for Identity Changes)**

- Used for: Core belief changes, occupation changes, address moves (STABLE facts)
- Behavior: Creates NEW document, marks old as `SUPERSEDED`
- Version: New document gets `version = old.version + 1`, `replaces_fact_id = old.id`
- SCD Type 2: Old fact gets `is_current = False`, `valid_to = now`, `state = SUPERSEDED`
- Example: "User is vegetarian" → corrected to "User eats meat now" → two separate documents

**When to Use Each Mode:**

- **In-Place:** Time series data (HEALTH, FINANCE metrics), adding complementary details
- **SCD Type 2:** Correcting core facts (BIOGRAPHICAL, WORK, LOCATION), invalidating beliefs

**Implementation Note:** The adapter (FirestoreFactManagementAdapter) determines mode automatically:

- If `state = SUPERSEDED` in updates → SCD Type 2 mode
- Otherwise → In-Place mode

**Example (In-Place):**

```python
# Existing: "User's current weight is 81 kg (Feb 7)"
# Update with new measurement:

result = await update_fact(
    fact_id="weight-fact-123",
    updates={
        "content": "User's weight: 80.5 kg (Feb 16), was 81 kg (Feb 7), 82.1 kg (Feb 5)",
        "tags": ["weight", "health", "biometrics", "tracking", "loss"],
        "reported_date": datetime.now()
    }
)
# → Same document updated, version incremented to 2
```

**Example (SCD Type 2 Correction):**

```python
# Existing: "User works as Software Engineer at Google"
# Correction: User changed job

result = await update_fact(
    fact_id="job-fact-456",
    updates={
        "state": "SUPERSEDED"  # Triggers SCD Type 2 mode
    }
)
# → Old fact marked SUPERSEDED, agent then calls create_fact() for new job
```

---

### 5.4 Tool 4: merge_facts

**Purpose:** Consolidate multiple facts into one enriched fact.

**Signature:**

```python
async def merge_facts(
    fact_ids: List[str],
    merged_content: str,
    metadata: Dict[str, Any]
) -> Dict[str, Any]
```

**Parameters:**

- `fact_ids` (List[str]) - UUIDs of facts to merge
- `merged_content` (str) - New combined text
- `metadata` (dict) - Metadata for new fact

**Returns:**

```python
{
    "new_fact_id": "merged-uuid-789",
    "old_fact_ids": ["uuid-1", "uuid-2"],
    "old_facts_state": "SUPERSEDED",
    "status": "merged",
    "message": "2 facts merged successfully"
}
```

**Example:**

```python
result = await merge_facts(
    fact_ids=["car-fact-1", "car-fact-2", "car-fact-3"],
    merged_content="User owns 2010 Toyota Corolla (Plate: XX0000YY) based in Example City, with automatic gearbox, tinted windows",
    metadata={
        "domain": "POSSESSION",
        "temporal_class": "STABLE",
        "state": "CURRENT",
        "tags": ["car", "vehicle", "mitsubishi", "puzol", "spain"],
        "reported_date": datetime.now()
    }
)
# Old facts marked as SUPERSEDED, new fact created
```

---

### 5.5 Tool 5: discard_candidate

**Purpose:** Explicitly reject candidate fact (not stored).

**Signature:**

```python
async def discard_candidate(
    reason: str
) -> Dict[str, Any]
```

**Parameters:**

- `reason` (str) - Explanation for rejection

**Returns:**

```python
{
    "status": "discarded",
    "reason": "Too vague, lacks actionable detail"
}
```

**Example:**

```python
# Candidate: "User is interested in AI"
result = await discard_candidate(
    reason="Too vague - no specific context or actionable information"
)
```

**Rationale:** Explicit rejection is better than silent ignoring. Helps with debugging and quality monitoring.

---

## 6. Tool Call Architecture

### 6.1 Provider-Agnostic Tool Execution

ConsolidationAgent v3 uses **custom tools** (not native provider tools) to maintain provider independence. Tools are declared in Groovy DSL and resolved by LLM adapters.

**Execution Pattern:**

```
ConsolidationAgent.execute()
  → Assemble prompt (with tool declarations)
  → LLM Provider Adapter (Claude/Gemini)
    → Translate custom tools → provider format
    → Execute multi-turn conversation
    → Return results
  → Parse tool results
  → Continue until Report step
```

**Multi-Turn Tool Use Loop:**

```python
# Pseudo-code (similar to SmartResponseAgent)
async def execute(self, message: AgentMessage):
    prompt = await self._build_consolidation_prompt(message)
    conversation_history = [{"role": "user", "content": prompt}]

    max_iterations = 10
    for iteration in range(max_iterations):
        response = await self.llm.generate_content(
            messages=conversation_history,
            tools=self._get_tool_declarations()  # Custom tools
        )

        if response.has_tool_calls():
            # Execute tool calls via FactManagementPort
            tool_results = await self._execute_tool_calls(response.tool_calls)
            conversation_history.append(tool_results)
        else:
            # Final report (Step 8)
            return self._parse_final_report(response.text)

    raise TimeoutError("Max iterations reached")
```

**Tool Declaration Format:**

Tools are declared in Groovy DSL (Section 6.2) and automatically translated by LLM adapters:

- `ClaudeAdapter`: Translates to Anthropic tool format
- `GeminiAdapter`: Translates to Google AI function calling format

**Error Handling:**

- Tool call failure → Retry with exponential backoff (3 attempts)
- Timeout → Log partial results, mark consolidation as incomplete
- Invalid tool response → Skip fact, continue with next candidate

---

## 7. Consolidation Agent Prompt

### 7.1 Complete Prompt (Groovy DSL)

```groovy
/**
 * Deliberate Fact Management Agent (Consolidation Architect v3.0)
 *
 * PURPOSE: Transform conversations into high-quality biographical memory that powers TWO critical systems:
 *
 * 1. BIOGRAPHICAL BASELINE CONTEXT
 *    - Injected into EVERY LLM prompt across ALL agents
 *    - Must contain user's name, gender, core identity (CRITICAL priority facts)
 *    - Built via simple priority-sorted query (not LLM-driven)
 *    - Used by: Response Agent, Router Agent, Tool Agents, Search Agent, etc.
 *
 * 2. SEMANTIC SEARCH RETRIEVAL
 *    - Retrieved ON-DEMAND based on conversation topic
 *    - Router Agent generates search query dynamically
 *    - Domain-filtered, priority-aware semantic search
 *    - Used for: Health queries, project details, specific questions
 *
 * YOUR RESPONSIBILITY: Classify facts on 4 axes so future systems can:
 *    - Filter by Domain → "Show me HEALTH facts"
 *    - Filter by TemporalClass → "Show me EPHEMERAL work"
 *    - Filter by State → "Show me CURRENT facts only"
 *    - Filter by ContextPriority → "Build baseline from CRITICAL+HIGH"
 *
 * Mode: Deliberate Curator (SLOW but THOUGHTFUL)
 * Specialty: Awareness-First, 4D Taxonomy, Lifecycle-Managed
 * Philosophy: "Every fact is a commitment. Quality over speed."
 */
class DeliberateFactCurator extends Agent {

    // =================================================================
    // A. RUNTIME CONTEXT (Input Data)
    // =================================================================

    runtime_context {
        /**
         * INPUT: Conversation transcript to analyze
         * Format: Structured list of messages with role/content/timestamp
         */
        conversation_history: {CONVERSATION_HISTORY}

        /**
         * CONTEXT: Known biographical facts (from Biographical Context Cache)
         * Source: Pre-cached baseline facts filtered by CRITICAL+HIGH priority
         * Limits: Configurable per account via ConfigurationService (default: ~50 facts + ~15 anchors)
         *
         * Use for:
         * - Entity disambiguation ("he" → specific person name from context)
         * - Relationship understanding (family structure, network)
         * - Avoiding exact duplication (check before creating similar facts)
         *
         * DO NOT:
         * - Insert names/details from context that aren't mentioned in conversation
         * - Assume context is current (facts can be outdated, check via SEARCH)
         * - Auto-fill missing information without user confirmation
         */
        biographical_context: {BIOGRAPHICAL_CONTEXT}

        /**
         * CONTEXT: Existing life principles (anchors)
         * Use for semantic deduplication
         */
        existing_anchors: [
{EXISTING_ANCHORS}
        ]
    }

    // =================================================================
    // B. KNOWLEDGE BASE (Taxonomy + Rules)
    // =================================================================

    knowledge_base {

        /**
         * TAXONOMY: 4-Dimensional Fact Classification
         * ALL facts MUST be classified on these 4 axes
         */
        fact_taxonomy {

            // AXIS 1: Domain (Semantic Category) - 15 predefined domains
            domains: {
                BIOGRAPHICAL: "Immutable identity (birthdate, blood type, citizenship, origin)"
                POSSESSION: "Owned physical objects (car, house, furniture, clothing, gadgets)"
                HEALTH: "Medical conditions, biometrics (weight, allergies, symptoms, chronic conditions)"
                MEDICAL_RECORDS: "Clinical data (lab results, x-rays, prescriptions, surgeries, diagnoses)"
                LOCATION: "Addresses, residence, travel (home, office, city, country, trips)"
                WORK: "Occupation, career, employment (job title, company, salary, methodologies)"
                NETWORK: "Contacts, relationships (family, friends, colleagues, mentors)"
                PREFERENCE: "Habits, likes, dislikes, anchors (food, values, principles, routines)"
                SKILL: "Abilities, knowledge, languages (programming, certifications, tools)"
                PROJECT: "Active work, temporary endeavors (current projects, evaluations, experiments) - USUALLY EPHEMERAL"
                FINANCE: "Money matters (income, expenses, investments, savings, debts, budgets)"
                EDUCATION: "Learning, degrees, courses (university, certifications, training, reading)"
                LEGAL: "Legal matters (contracts, agreements, licenses, legal issues, rights)"
                ENTERTAINMENT: "Leisure, hobbies, media (books, movies, games, sports, music)"
                COMMUNICATION: "Contact info, social media (phone, email, social accounts, handles)"
            }

            // AXIS 2: Temporal Class (Lifecycle)
            temporal_classes: {
                PERMANENT: {
                    description: "Cannot change by nature"
                    ttl: "None (forever)"
                    examples: ["birthdate", "blood type", "origin city"]
                }
                STABLE: {
                    description: "Rarely changes, versioned when updated"
                    ttl: "None (but versioned via SCD Type 2)"
                    examples: ["address", "occupation", "car ownership"]
                }
                DYNAMIC: {
                    description: "Changes regularly, short lifecycle"
                    ttl: "90 days"
                    examples: ["weight", "active projects", "reading list"]
                }
                EPHEMERAL: {
                    description: "Temporary state, very short lifecycle"
                    ttl: "14 days"
                    examples: ["evaluating tool X", "debugging API Y"]
                }
            }

            // AXIS 3: State (Actuality)
            states: {
                CURRENT: "Active, used now (default for new facts)"
                STALE: "Outdated, grace period (after TTL)"
                ARCHIVED: "Cold storage (extended inactivity)"
                SUPERSEDED: "Replaced by newer version"
                INVALIDATED: "Correction, no longer true"
            }

            // AXIS 4: Context Priority (Baseline Importance)
            context_priorities: {
                CRITICAL: {
                    description: "Essential for ANY conversation - affects how we ADDRESS user"
                    baseline: "Always included in biographical baseline context"
                    cognitive_test: "Would EVERY agent need this to communicate naturally?"
                    examples: ["name", "gender", "primary language", "core life anchor"]
                    guideline: "Be VERY selective. Truly CRITICAL means needed in 100% of conversations."
                }
                HIGH: {
                    description: "Needed for MOST conversations - provides primary life context"
                    baseline: "Usually included (if space permits)"
                    cognitive_test: "Would MOST conversations feel incomplete without this?"
                    examples: ["current job", "city/country", "family structure", "key skills"]
                    guideline: "Core context but not essential for every single interaction."
                }
                MEDIUM: {
                    description: "Useful for SOME conversations - adds detail"
                    baseline: "Sometimes included"
                    cognitive_test: "Would SOME conversations benefit from this?"
                    examples: ["possessions", "hobbies", "health metrics", "extended network"]
                    guideline: "Nice to have for specific topics."
                }
                LOW: {
                    description: "Query-specific only - retrieved on demand"
                    baseline: "Rarely included"
                    cognitive_test: "Only SPECIFIC queries need this?"
                    examples: ["temporary projects", "detailed preferences", "ephemeral work"]
                    guideline: "Retrieved via semantic search when relevant."
                }
                ARCHIVAL: {
                    description: "Historical, no longer relevant"
                    baseline: "Never included"
                    cognitive_test: "Is this obsolete?"
                    examples: ["superseded facts", "old versions", "invalidated data"]
                    guideline: "Automatically assigned to SUPERSEDED/INVALIDATED states."
                }
            }
        }

        /**
         * NEGATIVE CONSTRAINTS: What NOT to store
         */
        negative_constraints {

            @critical
            rule Trivial_Exclusions() {

                instruction: "NEVER store these categories - they pollute biographical memory"

                exclude: [
                    "Daily logistics: 'Going to store', 'Making coffee' (unless pattern-forming habit)",
                    "Emotional outbursts: 'I'm angry', 'This sucks' (unless chronic condition)",
                    "Polite chitchat: 'Hello', 'Thanks', 'See you', 'Good morning'",
                    "Meta conversation: 'What can you do?', 'How does this work?', 'Show me previous'",
                    "Ephemeral UI commands: 'Scroll up', 'Delete message', 'Edit that'",
                    "Questions without answers: 'What's my weight?' (only store answer if provided)",
                    "ASSISTANT recalls that user only confirms: RAG echo trap (no new info)",
                    "Temporary debugging state: 'Testing feature X' (unless ongoing project)"
                ]

                reasoning_test: "Would this fact be relevant in 30+ days?"

                if_no: "DISCARD immediately"
            }
        }

        /**
         * CONFLICT RESOLUTION: How to handle contradictory information
         */
        conflict_resolution {

            rule Time_Series_Data() {

                domains: ["HEALTH", "FINANCE", "SKILL"]

                policy: "Latest value UPDATES existing, preserves timeline history"

                examples: [
                    {
                        existing: "User's weight: 85 kg (Feb 5)",
                        new: "User weighs 82 kg",
                        action: "UPDATE",
                        result: "User's weight: 82 kg (Feb 16), was 85 kg (Feb 5)"
                    },
                    {
                        existing: "User's savings: $10,000 (Jan 2026)",
                        new: "User's savings: $12,500",
                        action: "UPDATE",
                        result: "User's savings: $12,500 (Feb 2026), was $10,000 (Jan 2026)"
                    }
                ]

                rationale: "Time series require history preservation, not replacement"
            }

            rule Core_Identity_Caution() {

                domains: ["BIOGRAPHICAL", "PREFERENCE"]

                policy: "Contradictory information requires strong evidence or explicit correction"

                examples: [
                    {
                        existing: "User is vegetarian",
                        new: "User ate steak",
                        action: "CREATE (separate observation, not UPDATE belief)",
                        reasoning: "May be exception, not identity change"
                    },
                    {
                        existing: "User is vegetarian",
                        new: "User: I'm no longer vegetarian, I eat meat now",
                        action: "UPDATE (explicit correction)",
                        result: "Mark old as SUPERSEDED, create new fact"
                    }
                ]

                rationale: "Core beliefs/identity rarely change - verify before overwriting"
            }

            rule Negation_And_Invalidation() {

                domains: ["ALL"]

                policy: "Detect explicit negations and invalidations, handle via state transitions"

                negation_patterns: [
                    "I no longer...",
                    "I don't... anymore",
                    "That's not true",
                    "I stopped...",
                    "I quit...",
                    "...is incorrect"
                ]

                examples: [
                    {
                        existing: "User works at Google",
                        new: "User: I no longer work at Google",
                        action: "UPDATE state=INVALIDATED on existing, optionally CREATE new fact if replacement mentioned",
                        reasoning: "Explicit negation detected"
                    },
                    {
                        existing: "User has diabetes",
                        new: "User: That diagnosis was wrong, I don't have diabetes",
                        action: "UPDATE state=INVALIDATED",
                        reasoning: "Correction of previous information"
                    },
                    {
                        existing: "User enjoys jogging",
                        new: "User: I stopped jogging 3 months ago",
                        action: "UPDATE state=ARCHIVED (not INVALIDATED - was true, now past)",
                        reasoning: "Temporal end of activity, not correction"
                    }
                ]

                decision_logic: {
                    INVALIDATED: "Use when fact was NEVER true or recorded incorrectly"
                    ARCHIVED: "Use when fact WAS true but is no longer current"
                    SUPERSEDED: "Use when fact is replaced by updated version (e.g., new job)"
                }

                rationale: "Explicit handling prevents accumulation of contradictory facts"
            }
        }

        /**
         * DECISION HEURISTICS: When to UPDATE vs CREATE vs MERGE vs DISCARD
         */
        decision_heuristics {

            update_conditions: [
                "Candidate adds data points to time series (e.g., new weight measurement)",
                "Candidate provides missing details to existing fact",
                "Candidate corrects existing fact (mark old as SUPERSEDED)"
            ]

            create_conditions: [
                "Candidate is orthogonal (different aspect of same domain)",
                "Candidate is new entity (new possession, new contact)",
                "Candidate is different time period (different event)"
            ]

            merge_conditions: [
                "Multiple facts describe same entity (car details scattered)",
                "Facts are complementary (can combine without contradiction)",
                "Combined fact is more useful than separate facts"
            ]

            discard_conditions: [
                "Candidate is EXACT duplicate",
                "Candidate is too vague (no actionable detail)",
                "Candidate is ephemeral question, not a fact",
                "Candidate adds zero new information"
            ]
        }

        /**
         * QUALITY RULES: Standards for fact formulation
         */
        quality_rules: [
            "Be SPECIFIC: 'User's weight is 80.5 kg' not 'User is losing weight'",
            "Be ATOMIC: One fact per entity/metric/statement",
            "Be DATED: Always include reported_date (conversation timestamp)",
            "Be CONTEXTUAL: Add temporal context for EPHEMERAL/DYNAMIC facts",
            "Be CONSERVATIVE: When unsure, CREATE (not UPDATE). Better separate than corrupted.",
            "Be RUTHLESS: Discard vague/redundant facts without hesitation",
            "Be PERSONALIZED: Use 'User's X' or '{UserName}'s X', never 'my X'"
        ]
    }

    // =================================================================
    // C. COGNITIVE PROCESS (8-Step Deliberation)
    // =================================================================

    cognitive_process {
        instruction: "Execute ALL steps sequentially. Think SLOWLY and DELIBERATELY. Use <thinking> tags to explain reasoning."

        execution_context: "This is a BACKGROUND JOB. User does not wait for your response. PRIORITIZE QUALITY (tokens, reasoning depth) over SPEED. Generate as many reasoning tokens as needed for confident decisions."

        reasoning_depth: {
            minimum: "5 explicit reasoning steps per candidate fact"
            format: "Step 1: [Extract], Step 2: [Classify], Step 3: [Search Analysis], Step 4: [Decision Logic], Step 5: [Verification]"
            instruction: "Use <thinking> tags extensively. Show your work."
        }

        steps: [
            "1. EXTRACT: Parse conversation → list of candidate facts",

            "2. CLASSIFY: For EACH candidate:",
            "   - Assign Domain (from predefined list)",
            "   - Assign Temporal Class (PERMANENT/STABLE/DYNAMIC/EPHEMERAL)",
            "   - Assign Initial State (CURRENT)",
            "   - Determine TTL (based on Temporal Class)",
            "   - Extract tags (domain-specific keywords)",
            "   - Add context (time period, trigger event)",

            "3. SEARCH: For EACH candidate:",
            "   <thinking>",
            "   Query existing facts to check for:",
            "   - Exact duplicates (skip)",
            "   - Similar facts (UPDATE candidate)",
            "   - Related facts (MERGE candidate)",
            "   - Orthogonal facts (CREATE new)",
            "   </thinking>",
            "   Call: search_existing_facts(",
            "       query=candidate.content,",
            "       domain=candidate.domain,",
            "       limit=10",
            "   )",

            "4. ANALYZE: For EACH candidate + search results:",
            "   <thinking>",
            "   Compare candidate to top 3 search results:",
            "   - Similarity score > 0.95 → EXACT duplicate",
            "   - Similarity 0.80-0.95 + same metric → UPDATE",
            "   - Similarity 0.80-0.95 + different aspect → CREATE",
            "   - Similarity < 0.80 → CREATE",
            "   - Multiple high-similarity results → MERGE",
            "   </thinking>",

            "5. DECIDE: Choose operation for EACH candidate:",
            "   <thinking>",
            "   Based on analysis:",
            "   - UPDATE: Add new data point to existing fact",
            "   - CREATE: New orthogonal information",
            "   - MERGE: Consolidate multiple facts",
            "   - DISCARD: No value added",
            "   Explain decision with reference to heuristics.",
            "   </thinking>",

            "6. EXECUTE: Call appropriate tool:",
            "   - create_fact(content, metadata)",
            "   - update_fact(fact_id, updates)",
            "   - merge_facts(fact_ids, merged_content, metadata)",
            "   - discard_candidate(reason)",

            "7. VERIFY: Check tool call result",
            "   - Confirm success",
            "   - Log fact_id for reference",

            "8. REPORT: Summarize actions taken",
            "   Format: {",
            "       \"operations\": [",
            "           {\"action\": \"UPDATE\", \"fact_id\": \"...\", \"reason\": \"...\"},",
            "           {\"action\": \"CREATE\", \"fact_id\": \"...\", \"reason\": \"...\"},",
            "           {\"action\": \"DISCARD\", \"reason\": \"...\"}",
            "       ]",
            "   }"
        ]
    }

    // =================================================================
    // D. TOOLS (5 Fact Management Operations)
    // =================================================================

    tools {

        @tool search_existing_facts(query: str, domain: str = null, limit: int = 10) {
            description: "Search Firestore for existing facts using semantic similarity"

            parameters: {
                query: "Semantic search query (candidate fact content)"
                domain: "Optional: filter by domain (BIOGRAPHICAL, HEALTH, etc.)"
                limit: "Max results to return (default: 10)"
            }

            returns: "List[Dict] with structure: {fact_id: UUID (REQUIRED for update_fact/merge_facts), content: str, domain: str, temporal: str, state: str, tags: List[str], similarity: float}"

            usage_example: '''
            # Search for weight-related facts
            results = search_existing_facts(
                query="user weight 81 kg biometrics",
                domain="HEALTH",
                limit=10
            )
            '''
        }

        @tool create_fact(content: str, metadata: dict) {
            description: "Create NEW fact when candidate is orthogonal or new entity"

            parameters: {
                content: "Fact text (self-contained sentence)"
                metadata: {
                    domain: "FactDomain (BIOGRAPHICAL, POSSESSION, etc.)"
                    temporal_class: "TemporalClass (PERMANENT, STABLE, DYNAMIC, EPHEMERAL)"
                    state: "FactState (default: CURRENT)"
                    context_priority: "ContextPriority (CRITICAL, HIGH, MEDIUM, LOW, ARCHIVAL) - MANDATORY for Baseline Context filtering"
                    ttl_days: "Auto-calculated from temporal_class (can override)"
                    tags: "List of domain keywords"
                    context: "Optional temporal context (e.g., 'Q1 2026 project')"
                    reported_date: "When fact was recorded (now)"
                }
            }

            returns: "{fact_id, status, message}"

            usage_example: '''
            result = create_fact(
                content="User started morning yoga practice (30 min daily)",
                metadata={
                    "domain": "HEALTH",
                    "temporal_class": "DYNAMIC",
                    "state": "CURRENT",
                    "ttl_days": 90,
                    "tags": ["yoga", "habit", "health", "morning"],
                    "context": "Q1 2026 habit formation",
                    "reported_date": "2026-02-16T10:00:00"
                }
            )
            '''
        }

        @tool update_fact(fact_id: str, updates: dict) {
            description: "Update EXISTING fact (enrichment or new data point)"

            parameters: {
                fact_id: "UUID of fact to update"
                updates: {
                    content: "Optional: new/enriched text"
                    tags: "Optional: add tags"
                    temporal_class: "Optional: can upgrade/downgrade"
                    state: "Optional: change state"
                    reported_date: "Always update to now"
                }
            }

            returns: "{fact_id, status, version, message}"

            usage_example: '''
            # Add new weight measurement to existing fact
            result = update_fact(
                fact_id="weight-fact-123",
                updates={
                    "content": "User's weight: 80.5 kg (Feb 16), was 81 kg (Feb 7), 82.1 kg (Feb 5)",
                    "tags": ["weight", "health", "biometrics", "tracking", "loss"],
                    "reported_date": "2026-02-16T10:00:00"
                }
            )
            '''
        }

        @tool merge_facts(fact_ids: list, merged_content: str, metadata: dict) {
            description: "Consolidate multiple facts into one enriched fact"

            parameters: {
                fact_ids: "List of UUIDs to merge"
                merged_content: "New combined text"
                metadata: "Metadata for new fact (MUST include: domain, temporal_class, state, context_priority, tags, reported_date)"
            }

            returns: "{new_fact_id, old_fact_ids, old_facts_state: SUPERSEDED, status, message}"

            usage_example: '''
            result = merge_facts(
                fact_ids=["car-1", "car-2", "car-3"],
                merged_content="User owns 2010 Toyota Corolla (Plate: XX0000YY) based in Example City, with automatic gearbox, tinted windows",
                metadata={
                    "domain": "POSSESSION",
                    "temporal_class": "STABLE",
                    "state": "CURRENT",
                    "tags": ["car", "mitsubishi", "puzol", "spain"],
                    "reported_date": "2026-02-16T10:00:00"
                }
            )
            '''
        }

        @tool discard_candidate(reason: str) {
            description: "Explicitly reject candidate fact (not stored)"

            parameters: {
                reason: "Explanation for rejection"
            }

            returns: "{status: discarded, reason}"

            usage_example: '''
            # Candidate too vague
            result = discard_candidate(
                reason="Too vague - 'User is interested in AI' lacks specific context"
            )
            '''
        }
    }

    // =================================================================
    // E. EXAMPLES (Reasoning Traces)
    // =================================================================

    examples {

        example_1_update_weight: {
            conversation: "USER: Yesterday I weighed 80.5 kg"

            reasoning: '''
            <thinking>
            STEP 1 - EXTRACT:
            - Candidate: "User's weight is 80.5 kg (Feb 16, 2026)"

            STEP 2 - CLASSIFY:
            - Domain: HEALTH
            - Temporal: DYNAMIC (weight changes regularly)
            - State: CURRENT
            - TTL: 90 days
            - Tags: ["weight", "health", "biometrics", "tracking"]

            STEP 3 - SEARCH:
            Query: "user weight kg biometrics"
            Domain: HEALTH
            Results: [
                {
                    fact_id: "weight-123",
                    content: "User's current weight is 81 kg (Feb 7)",
                    similarity: 0.95
                }
            ]

            STEP 4 - ANALYZE:
            - Top result: 95% similarity
            - Same metric (weight in kg)
            - Different data point (new measurement, 9 days later)
            - Match type: TIME SERIES UPDATE

            STEP 5 - DECIDE:
            Operation: UPDATE fact_id="weight-123"
            Reason: New data point in time series (weight tracking)
            Heuristic: "Candidate adds data points to time series"

            STEP 6 - EXECUTE:
            update_fact(
                fact_id="weight-123",
                updates={
                    "content": "User's weight: 80.5 kg (Feb 16), was 81 kg (Feb 7), 82.1 kg (Feb 5)",
                    "tags": ["weight", "health", "biometrics", "tracking", "loss"],
                    "reported_date": "2026-02-16T10:00:00"
                }
            )

            STEP 7 - VERIFY:
            ✓ Fact updated successfully (version 2)

            STEP 8 - REPORT:
            Action: UPDATE weight fact with new measurement
            </thinking>
            '''

            output: {
                "operations": [
                    {
                        "action": "UPDATE",
                        "fact_id": "weight-123",
                        "reason": "New data point in weight time series"
                    }
                ]
            }
        }

        example_2_create_new_habit: {
            conversation: "USER: I started practicing morning yoga, 30 minutes every day"

            reasoning: '''
            <thinking>
            STEP 1 - EXTRACT:
            - Candidate: "User started morning yoga practice (30 min daily)"

            STEP 2 - CLASSIFY:
            - Domain: HEALTH
            - Temporal: DYNAMIC (new habit, may change)
            - State: CURRENT
            - TTL: 90 days
            - Tags: ["yoga", "habit", "health", "morning", "routine"]
            - Context: "Q1 2026 habit formation"

            STEP 3 - SEARCH:
            Query: "user yoga morning practice habit"
            Domain: HEALTH
            Results: [] (no matches)

            STEP 4 - ANALYZE:
            - No existing facts about yoga
            - Match type: NO MATCH

            STEP 5 - DECIDE:
            Operation: CREATE new fact
            Reason: New entity (yoga habit not previously recorded)
            Heuristic: "Candidate is new entity"

            STEP 6 - EXECUTE:
            create_fact(
                content="User started morning yoga practice (30 min daily) in Q1 2026",
                metadata={
                    "domain": "HEALTH",
                    "temporal_class": "DYNAMIC",
                    "state": "CURRENT",
                    "ttl_days": 90,
                    "tags": ["yoga", "habit", "health", "morning", "routine"],
                    "context": "Q1 2026 habit formation",
                    "reported_date": "2026-02-16T10:00:00"
                }
            )

            STEP 7 - VERIFY:
            ✓ Fact created successfully (fact_id: yoga-habit-456)

            STEP 8 - REPORT:
            Action: CREATE new yoga habit fact
            </thinking>
            '''

            output: {
                "operations": [
                    {
                        "action": "CREATE",
                        "fact_id": "yoga-habit-456",
                        "reason": "New habit not previously recorded"
                    }
                ]
            }
        }

        example_3_discard_echo: {
            conversation: '''
            ASSISTANT: Based on our history, you weigh 82kg.
            USER: Yes, exactly.
            '''

            reasoning: '''
            <thinking>
            STEP 1 - EXTRACT:
            - Candidate: "User confirms weight is 82kg"

            STEP 2 - CLASSIFY:
            - Domain: HEALTH
            - Temporal: DYNAMIC
            - State: CURRENT

            STEP 3 - SEARCH:
            Query: "user weight 82kg"
            Domain: HEALTH
            Results: [
                {
                    fact_id: "weight-123",
                    content: "User's weight is 82kg",
                    similarity: 0.98  # Almost exact match
                }
            ]

            STEP 4 - ANALYZE:
            - Top result: 98% similarity
            - ASSISTANT stated fact first (from RAG memory)
            - USER only confirmed, added NO new information
            - Match type: RAG ECHO TRAP

            STEP 5 - DECIDE:
            Operation: DISCARD
            Reason: User confirmation of ASSISTANT recall - no new information
            Heuristic: "Candidate is EXACT duplicate" + "Echo trap"

            STEP 6 - EXECUTE:
            discard_candidate(
                reason="User confirmation of ASSISTANT recall - no new information (RAG echo trap)"
            )

            STEP 7 - VERIFY:
            ✓ Candidate discarded

            STEP 8 - REPORT:
            Action: DISCARD (echo trap)
            </thinking>
            '''

            output: {
                "operations": [
                    {
                        "action": "DISCARD",
                        "reason": "RAG echo trap - User confirmed existing fact without adding new info"
                    }
                ]
            }
        }

        example_4_merge_car_details: {
            conversation: "USER: My car has tinted windows"

            reasoning: '''
            <thinking>
            STEP 1 - EXTRACT:
            - Candidate: "User's car has tinted windows"

            STEP 2 - CLASSIFY:
            - Domain: POSSESSION
            - Temporal: STABLE (car features don't change often)
            - State: CURRENT
            - Tags: ["car", "features", "accessories"]

            STEP 3 - SEARCH:
            Query: "user car mitsubishi colt features"
            Domain: POSSESSION
            Results: [
                {
                    fact_id: "car-1",
                    content: "User owns 2010 Toyota Corolla",
                    similarity: 0.85
                },
                {
                    fact_id: "car-2",
                    content: "Toyota Corolla has automatic gearbox",
                    similarity: 0.82
                },
                {
                    fact_id: "car-3",
                    content: "Car is in Example City",
                    similarity: 0.78
                }
            ]

            STEP 4 - ANALYZE:
            - Multiple facts (3) about SAME entity (Toyota Corolla)
            - All high similarity (>0.75)
            - Facts are complementary (different aspects)
            - Combined fact would be more useful
            - Match type: MULTIPLE SIMILAR (same entity, different details)

            STEP 5 - DECIDE:
            Operation: MERGE car-1 + car-2 + car-3 + new info
            Reason: Consolidate scattered car details into one comprehensive fact
            Heuristic: "Multiple facts describe same entity" + "Facts are complementary"

            STEP 6 - EXECUTE:
            merge_facts(
                fact_ids=["car-1", "car-2", "car-3"],
                merged_content="User owns 2010 Toyota Corolla (Plate: XX0000YY) based in Example City, with automatic gearbox, tinted windows",
                metadata={
                    "domain": "POSSESSION",
                    "temporal_class": "STABLE",
                    "state": "CURRENT",
                    "tags": ["car", "vehicle", "mitsubishi", "puzol", "spain", "features"],
                    "reported_date": "2026-02-16T10:00:00"
                }
            )

            STEP 7 - VERIFY:
            ✓ Facts merged successfully
            ✓ Old facts marked as SUPERSEDED
            ✓ New fact created (fact_id: car-merged-789)

            STEP 8 - REPORT:
            Action: MERGE 3 car facts + new details into comprehensive fact
            </thinking>
            '''

            output: {
                "operations": [
                    {
                        "action": "MERGE",
                        "new_fact_id": "car-merged-789",
                        "old_fact_ids": ["car-1", "car-2", "car-3"],
                        "reason": "Consolidated scattered car details into one comprehensive fact"
                    }
                ]
            }
        }
    }

    // =================================================================
    // F. POLICIES (Hard Constraints)
    // =================================================================

    policies {

        @critical
        rule Domain_Scope() {
            definition: "Strict boundaries for fact extraction"

            constraints: [
                "EXTRACT all facts relevant to USER's life context",
                "NEVER extract external world facts unless USER-specific",
                "NEVER process ASSISTANT statements as facts unless USER confirms with NEW info",
                "NEVER create facts from questions (extract only answers)"
            ]

            fallback: "When in doubt, DISCARD with explanation"
        }

        @critical
        rule Tool_Call_Mandatory() {
            definition: "ALL operations MUST use tools"

            constraints: [
                "NEVER describe what you would do - CALL the tool",
                "NEVER return facts without calling create_fact or update_fact",
                "NEVER skip SEARCH step - always check existing facts first"
            ]

            fallback: "If tool call fails, report error and STOP"
        }

        @critical
        rule Taxonomy_Enforcement() {
            definition: "ALL facts MUST be classified on 4 axes"

            constraints: [
                "Domain: MUST be from predefined list (see knowledge_base.fact_taxonomy.domains)",
                "Temporal Class: MUST be PERMANENT/STABLE/DYNAMIC/EPHEMERAL",
                "State: MUST be CURRENT/STALE/ARCHIVED/SUPERSEDED/INVALIDATED",
                "Context Priority: MUST be CRITICAL/HIGH/MEDIUM/LOW/ARCHIVAL"
            ]

            fallback: "If classification unclear, use conservative defaults (STABLE, CURRENT, MEDIUM)"
        }
    }

    // =================================================================
    // G. OUTPUT SPECIFICATION
    // =================================================================

    output_specification {

        format: "JSON object with operations list"

        schema: {
            operations: "Array of operation objects"
        }

        operation_object: {
            action: "Enum: CREATE | UPDATE | MERGE | DISCARD"
            fact_id: "String (for UPDATE/MERGE) - UUID of affected fact"
            new_fact_id: "String (for MERGE) - UUID of newly created merged fact"
            old_fact_ids: "Array (for MERGE) - UUIDs of superseded facts"
            reason: "String - Explanation for decision"
        }

        example_output: '''
        {
            "operations": [
                {
                    "action": "UPDATE",
                    "fact_id": "weight-123",
                    "reason": "Added new weight measurement to time series"
                },
                {
                    "action": "CREATE",
                    "fact_id": "yoga-456",
                    "reason": "New habit not previously recorded"
                },
                {
                    "action": "DISCARD",
                    "reason": "Too vague - no actionable detail"
                },
                {
                    "action": "MERGE",
                    "new_fact_id": "car-789",
                    "old_fact_ids": ["car-1", "car-2", "car-3"],
                    "reason": "Consolidated scattered car details"
                }
            ]
        }
        '''
    }
}
```

---

## 7. Hexagonal Architecture

### 7.1 Layer Responsibilities

```
┌─────────────────────────────────────────────────────────────┐
│                     DOMAIN LAYER                             │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ FactEntity (with new fields)                           │ │
│  │ - domain: FactDomain                                   │ │
│  │ - temporal_class: TemporalClass                        │ │
│  │ - state: FactState                                     │ │
│  │ - ttl_days, last_updated, expires_at                   │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ ConsolidationAgent (receives tools via DI)             │ │
│  │ - execute(message) → orchestrates 8-step process       │ │
│  │ - Uses FactManagementPort (injected)                   │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                           ▲
                           │ depends on
                           │
┌─────────────────────────────────────────────────────────────┐
│                    PORTS LAYER (Interfaces)                  │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ FactManagementPort (ABC)                               │ │
│  │ - search_existing_facts(...)                           │ │
│  │ - create_fact(...)                                     │ │
│  │ - update_fact(...)                                     │ │
│  │ - merge_facts(...)                                     │ │
│  │ - discard_candidate(...)                               │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                           ▲
                           │ implements
                           │
┌─────────────────────────────────────────────────────────────┐
│              APPLICATION/INFRASTRUCTURE LAYER                │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ FirestoreFactManagementAdapter (implements Port)       │ │
│  │ - search_existing_facts → Firestore vector search      │ │
│  │ - create_fact → Firestore write + embedding           │ │
│  │ - update_fact → Firestore update + version increment  │ │
│  │ - merge_facts → Multi-doc transaction                 │ │
│  │ - discard_candidate → No-op (logging only)            │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ FactLifecycleService (background job)                  │ │
│  │ - Daily job: transition states (CURRENT→STALE→ARCHIVED) │
│  │ - TTL enforcement                                       │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

### 7.2 File Structure

```
src/
├── domain/
│   ├── entities.py
│   │   └── FactEntity (extended with new fields)
│   ├── enums.py
│   │   ├── FactDomain
│   │   ├── TemporalClass
│   │   └── FactState
│   └── ...
│
├── ports/
│   ├── fact_management_port.py  # NEW
│   │   └── FactManagementPort (ABC with 5 methods)
│   └── ...
│
├── adapters/
│   ├── firestore_fact_management_adapter.py  # NEW
│   │   └── FirestoreFactManagementAdapter (implements Port)
│   └── ...
│
├── services/
│   ├── fact_lifecycle_service.py  # NEW
│   │   └── FactLifecycleService (background job)
│   └── ...
│
├── agents/
│   ├── consolidation_agent.py  # MODIFIED
│   │   └── ConsolidationAgent (uses FactManagementPort)
│   └── prompts/
│       └── consolidation_v3.prompt  # NEW
│
└── ...
```

---

### 7.3 FactWriteService Integration

**Purpose:** FirestoreFactManagementAdapter reuses `FactWriteService` for multi-vector embedding generation while adding deduplication-free mode for ConsolidationAgent v3.

**Why FactWriteService:**

- Already implements proven 3-vector embedding strategy (text, tags, metadata)
- Handles batch processing and Firestore writes efficiently
- Maintains backward compatibility for legacy consolidation v2

**New Mode: `skip_deduplication=True`**

ConsolidationAgent v3 performs deduplication via `search_existing_facts` tool (Step 3: SEARCH). FactWriteService should NOT perform additional semantic dedup when called by v3 agent.

**FactWriteService Enhancement:**

```python
# src/services/fact_write_service.py

class FactWriteService:

    async def add_facts_batch(
        self,
        facts: List[FactEntity],
        skip_deduplication: bool = False  # NEW parameter
    ) -> List[str]:
        """
        Write facts to Firestore with multi-vector embeddings.

        Args:
            facts: List of FactEntity objects
            skip_deduplication: If True, skip semantic dedup check (for v3 agent)

        Returns:
            List of created fact IDs
        """

        # Generate embeddings
        for fact in facts:
            fact.vector = await self._generate_text_embedding(fact.text)
            fact.tags_vector = await self._generate_tags_embedding(fact.tags)
            fact.metadata_vector = await self._generate_metadata_embedding(fact.metadata)

        # Deduplication (skippable for v3 agent)
        if not skip_deduplication:
            facts = await self._deduplicate_facts(facts)

        # Batch write to Firestore
        return await self._write_facts_batch(facts)
```

**Adapter Usage:**

```python
# src/adapters/firestore_fact_management_adapter.py

class FirestoreFactManagementAdapter(FactManagementPort):

    def __init__(self, db, embedding_service, fact_write_service):
        self.db = db
        self.embedding_service = embedding_service
        self.fact_write_service = fact_write_service  # Injected

    async def create_fact(self, content: str, metadata: Dict) -> Dict:
        """Create new fact using FactWriteService (no dedup)."""

        # Build FactEntity from parameters
        fact = FactEntity(
            text=content,
            domain=metadata["domain"],
            temporal_class=metadata["temporal_class"],
            state=metadata.get("state", "CURRENT"),
            context_priority=metadata["context_priority"],
            tags=metadata.get("tags", []),
            ttl_days=metadata.get("ttl_days"),
            context=metadata.get("context"),
            reported_date=metadata["reported_date"],
            account_id=metadata["account_id"],
            created_by_user_id=metadata["user_id"],
            lineage_id=str(uuid4())
        )

        # Write via FactWriteService (skip dedup - already done by agent)
        fact_ids = await self.fact_write_service.add_facts_batch(
            facts=[fact],
            skip_deduplication=True  # Agent already searched existing facts
        )

        return {
            "fact_id": fact_ids[0],
            "status": "created",
            "message": "Fact created successfully"
        }
```

---

### 7.4 Dependency Injection

```python
# main.py

# 1. Initialize FactWriteService (shared by v2 and v3)
fact_write_service = FactWriteService(
    repo=fact_repository,
    embedding_service=embedding_service
)

# 2. Initialize Port implementation (Adapter) - injects FactWriteService
fact_management_adapter = FirestoreFactManagementAdapter(
    db=firestore_client,
    embedding_service=embedding_service,
    fact_write_service=fact_write_service  # Reuse existing service
)

# 3. Initialize Background Service
fact_lifecycle_service = FactLifecycleService(
    fact_repo=fact_repository,
    check_interval_hours=24
)

# 4. Initialize Consolidation Agent (inject Port)
consolidation_agent = ConsolidationAgent(
    config=agent_config,
    execution_context=execution_context,
    repository=fact_repository,
    embedding_service=embedding_service,
    fact_write_service=fact_write_service,  # Legacy path (v2)
    fact_management_port=fact_management_adapter,  # NEW (v3)
    prompt_builder=prompt_builder
)

# 5. Start Background Service
asyncio.create_task(fact_lifecycle_service.start())
```

**Migration Path:**

- Phase 1-2: Both v2 (FactWriteService direct) and v3 (FactManagementPort) coexist
- Phase 3: Feature flag switches between v2 and v3 execution paths
- Phase 4: Remove v2 path after successful v3 validation

---

## 8. Implementation Plan

### Phase 1: Domain & Ports (Week 1)

**Goal:** Extend domain model + define interfaces

**Tasks:**

1. Add new fields to `FactEntity` (domain, temporal_class, state, ttl_days, etc.)
2. Create enums (`FactDomain`, `TemporalClass`, `FactState`)
3. Create `FactManagementPort` interface (5 methods)
4. Write unit tests for domain model extensions
5. Update Firestore schema documentation

**Deliverables:**

- `src/domain/entities.py` (updated)
- `src/domain/enums.py` (new enums)
- `src/ports/fact_management_port.py` (new port)
- `tests/unit/domain/test_fact_entity_extensions.py`

**Estimated Effort:** 16 hours

---

### Phase 2: Adapter Implementation (Week 2)

**Goal:** Implement Firestore adapter for 5 tools

**Tasks:**

1. Implement `FirestoreFactManagementAdapter`
   - `search_existing_facts` (vector search)
   - `create_fact` (write + embedding)
   - `update_fact` (atomic update + version increment)
   - `merge_facts` (transaction: mark old as SUPERSEDED, create new)
   - `discard_candidate` (no-op, logging only)
2. Add Firestore indexes for new fields (domain, temporal_class, state)
3. Write adapter unit tests (mocked Firestore)
4. Write adapter integration tests (real Firestore)

**Deliverables:**

- `src/adapters/firestore_fact_management_adapter.py`
- `config/firestore.indexes.json` (updated)
- `tests/unit/adapters/test_firestore_fact_management_adapter.py`
- `tests/integration/test_fact_management_adapter_e2e.py`

**Estimated Effort:** 24 hours

---

### Phase 3: Consolidation Agent Update (Week 3)

**Goal:** Integrate new prompt + tools into agent

**Tasks:**

1. Create `consolidation_v3.prompt` (Groovy DSL with 8-step process)
2. Update `ConsolidationAgent`:
   - Inject `FactManagementPort`
   - Call tools in execute() method
   - Parse tool results
   - Handle errors gracefully
3. Update prompt assembly (inject existing facts context)
4. Add <thinking> tag parsing (for Claude reasoning extraction)
5. Write agent unit tests (mocked tools)
6. Write agent integration tests (real tools)

**Deliverables:**

- `src/agents/prompts/consolidation_v3.prompt`
- `src/agents/consolidation_agent.py` (updated)
- `tests/unit/agents/test_consolidation_agent_v3.py`
- `tests/integration/test_consolidation_agent_v3_e2e.py`

**Estimated Effort:** 32 hours

---

### Phase 4: Lifecycle Service (Week 4)

**Goal:** Implement background job for state transitions

**Tasks:**

1. Create `FactLifecycleService`:
   - Daily cron job
   - Query facts by TTL
   - Transition states (CURRENT→STALE→ARCHIVED)
   - Update Firestore in batches
2. Add service to main.py startup
3. Add monitoring (log state transitions)
4. Write service unit tests
5. Write service integration tests

**Deliverables:**

- `src/services/fact_lifecycle_service.py`
- `main.py` (updated with background task)
- `tests/unit/services/test_fact_lifecycle_service.py`
- `tests/integration/test_fact_lifecycle_service_e2e.py`

**Estimated Effort:** 16 hours

---

### Phase 5: Migration & Deployment (Week 5)

**Goal:** Migrate existing facts + deploy to DEV

**Tasks:**

1. Create migration script:
   - Read existing facts
   - Infer taxonomy (domain from tags, temporal_class = STABLE)
   - Write new fields
   - Preserve old data
2. Run migration on DEV Firestore
3. Deploy Consolidation Agent v3 to DEV
4. Monitor consolidation quality (manual review)
5. Compare v2 vs v3 metrics (duplication rate, quality score)
6. Iterate on prompt based on results

**Deliverables:**

- `scripts/migration/migrate_facts_to_v3.py`
- Deployed to DEV environment
- Migration report (facts migrated, issues found)
- Quality comparison report

**Estimated Effort:** 16 hours

---

### Phase 6: Production Rollout (Week 6)

**Goal:** Deploy to PROD + monitor

**Tasks:**

1. Review DEV metrics (1 week observation)
2. Tune prompt based on DEV learnings
3. Run migration on PROD Firestore (backup first!)
4. Deploy to PROD (feature flag: `ENABLE_CONSOLIDATION_V3=true`)
5. Monitor for 2 weeks:
   - Duplication rate
   - Consolidation latency
   - Tool call success rate
   - User feedback
6. Document lessons learned

**Deliverables:**

- PROD deployment
- Monitoring dashboard
- Lessons learned document

**Estimated Effort:** 16 hours (+ 2 weeks monitoring)

---

### Summary

| Phase                   | Duration    | Effort        | Deliverables                       |
| ----------------------- | ----------- | ------------- | ---------------------------------- |
| Phase 1: Domain & Ports | Week 1      | 16h           | Domain extensions, Port interface  |
| Phase 2: Adapter        | Week 2      | 24h           | Firestore adapter, 5 tools         |
| Phase 3: Agent          | Week 3      | 32h           | Consolidation Agent v3, new prompt |
| Phase 4: Lifecycle      | Week 4      | 16h           | Background job, state transitions  |
| Phase 5: Migration      | Week 5      | 16h           | DEV deployment, testing            |
| Phase 6: Production     | Week 6      | 16h           | PROD deployment, monitoring        |
| **TOTAL**               | **6 weeks** | **120 hours** | **Full system**                    |

---

## 9. Testing Strategy

### 9.1 Unit Tests

**Domain Layer:**

```python
# tests/unit/domain/test_fact_entity_extensions.py

def test_fact_entity_with_taxonomy():
    fact = FactEntity(
        id="test-1",
        text="User weighs 81kg",
        domain=FactDomain.HEALTH,
        temporal_class=TemporalClass.DYNAMIC,
        state=FactState.CURRENT,
        ttl_days=90,
        tags=["weight", "health"]
    )

    assert fact.domain == FactDomain.HEALTH
    assert fact.temporal_class == TemporalClass.DYNAMIC
    assert fact.expires_at is not None  # Auto-calculated

def test_temporal_class_ttl_auto_calculation():
    # PERMANENT → no TTL
    fact_perm = FactEntity(..., temporal_class=TemporalClass.PERMANENT)
    assert fact_perm.ttl_days is None

    # DYNAMIC → 90 days
    fact_dyn = FactEntity(..., temporal_class=TemporalClass.DYNAMIC)
    assert fact_dyn.ttl_days == 90
```

---

**Adapter Layer:**

```python
# tests/unit/adapters/test_firestore_fact_management_adapter.py

@pytest.mark.asyncio
async def test_search_existing_facts():
    adapter = FirestoreFactManagementAdapter(...)

    results = await adapter.search_existing_facts(
        query="user weight 81kg",
        domain="HEALTH",
        limit=10
    )

    assert len(results) > 0
    assert results[0]["fact_id"] is not None
    assert results[0]["similarity"] > 0.7

@pytest.mark.asyncio
async def test_update_fact_increments_version():
    adapter = FirestoreFactManagementAdapter(...)

    # Create fact
    create_result = await adapter.create_fact(...)
    fact_id = create_result["fact_id"]

    # Update fact
    update_result = await adapter.update_fact(
        fact_id=fact_id,
        updates={"content": "Updated text"}
    )

    assert update_result["version"] == 2  # Incremented
```

---

### 9.2 Integration Tests

```python
# tests/integration/test_consolidation_agent_v3_e2e.py

@pytest.mark.asyncio
async def test_consolidation_agent_updates_existing_fact():
    """Test that agent UPDATES existing fact instead of creating duplicate."""

    # Setup: Create existing fact
    existing_fact = await fact_repo.add_fact(
        account_id="test-account",
        user_id="test-user",
        text="User's current weight is 81 kg",
        tags=["weight", "health"],
        domain="HEALTH",
        temporal_class="DYNAMIC"
    )

    # Execute: Consolidation with new weight measurement
    message = AgentMessage(
        task_id="test-task",
        intent=AgentIntent.DELEGATE,
        payload={
            "task": "consolidate",
            "messages": [
                {"role": "user", "content": "Yesterday I weighed 80.5 kg", "timestamp": "2026-02-16T10:00:00"}
            ]
        },
        context={"user_id": "test-user", "account_id": "test-account"}
    )

    response = await consolidation_agent.execute(message)

    # Verify: Fact was UPDATED (not created)
    assert response.success is True
    operations = response.metadata["operations"]

    assert len(operations) == 1
    assert operations[0]["action"] == "UPDATE"
    assert operations[0]["fact_id"] == existing_fact.id

    # Verify: Fact content enriched
    updated_fact = await fact_repo.get_fact_by_id(existing_fact.id)
    assert "80.5 kg" in updated_fact.text
    assert "81 kg" in updated_fact.text  # History preserved
```

---

### 9.3 Quality Tests

```python
# tests/quality/test_consolidation_quality.py

@pytest.mark.asyncio
async def test_no_duplicate_facts_created():
    """Test that consolidation doesn't create duplicates."""

    # Consolidate same conversation 3 times
    for _ in range(3):
        await consolidation_agent.execute(same_message)

    # Verify: Only 1 fact created (not 3)
    facts = await fact_repo.get_active_facts(user_id)
    assert len(facts) == 1

@pytest.mark.asyncio
async def test_ephemeral_facts_auto_archived():
    """Test that EPHEMERAL facts expire after 14 days."""

    # Create EPHEMERAL fact
    fact = await fact_repo.add_fact(
        text="User is evaluating Vertex AI",
        temporal_class="EPHEMERAL",
        ttl_days=14
    )

    # Simulate 15 days passing
    with freeze_time(datetime.now() + timedelta(days=15)):
        # Run lifecycle job
        await fact_lifecycle_service.process_expired_facts()

    # Verify: Fact marked as ARCHIVED
    updated_fact = await fact_repo.get_fact_by_id(fact.id)
    assert updated_fact.state == FactState.ARCHIVED
```

---

## 10. Risk Assessment

### 10.1 Technical Risks

| Risk                                    | Impact | Probability | Mitigation                                                               |
| --------------------------------------- | ------ | ----------- | ------------------------------------------------------------------------ |
| LLM doesn't follow 8-step process       | HIGH   | MEDIUM      | Detailed prompt with examples, use Claude (better instruction following) |
| Tool calls fail silently                | HIGH   | LOW         | Add retry logic, explicit error handling, logging                        |
| Firestore vector search slow            | MEDIUM | LOW         | Index optimization, query caching, pagination                            |
| Migration breaks existing facts         | HIGH   | LOW         | Backup before migration, dry-run mode, gradual rollout                   |
| Consolidation latency too high (>2 min) | MEDIUM | MEDIUM      | Optimize tool calls, parallel SEARCH, timeout handling                   |

---

### 10.2 Operational Risks

| Risk                                    | Impact | Probability | Mitigation                                                      |
| --------------------------------------- | ------ | ----------- | --------------------------------------------------------------- |
| Users complain about slow consolidation | MEDIUM | HIGH        | Set expectations (background process), show progress indicator  |
| Fact lifecycle job crashes              | MEDIUM | LOW         | Error handling, monitoring, alerting                            |
| Wrong facts archived                    | HIGH   | LOW         | Extend grace period (30→60 days), manual review UI              |
| Cost increase (more LLM calls)          | LOW    | HIGH        | Expected (3-7 tool calls per fact), acceptable for quality gain |

---

### 10.3 Acceptance Criteria

**Deployment Blockers:**

- [ ] Duplication rate < 5% (current: 15-20%)
- [ ] All 76 existing unit tests pass
- [ ] 20+ new unit tests added (domain + adapter + agent)
- [ ] 5+ integration tests pass (E2E flows)
- [ ] Migration script tested on DEV
- [ ] Firestore indexes created and READY
- [ ] Background job runs without errors for 1 week

**Quality Metrics (after 2 weeks in PROD):**

- [ ] Duplication rate < 2%
- [ ] Ephemeral facts < 5% of database
- [ ] Consolidation latency < 120s (P95)
- [ ] Tool call success rate > 95%
- [ ] Zero critical errors

---

## 11. Appendices

### A. Example Conversations & Expected Behavior

#### A.1 Weight Update (Time Series)

**Input:**

```
USER: Yesterday I weighed 80.5 kg
```

**Expected:**

- SEARCH finds: "User's current weight is 81 kg (Feb 7)"
- DECISION: UPDATE (time series)
- RESULT: "User's weight: 80.5 kg (Feb 16), was 81 kg (Feb 7)"

---

#### A.2 New Habit (Create)

**Input:**

```
USER: I started doing yoga in the mornings, 30 minutes
```

**Expected:**

- SEARCH finds: No yoga facts
- DECISION: CREATE
- RESULT: "User started morning yoga practice (30 min daily)"
- Taxonomy: HEALTH / DYNAMIC / CURRENT / TTL=90

---

#### A.3 Ephemeral Project (Create + Auto-Archive)

**Input:**

```
USER: I am integrating Gemini API into the project
```

**Expected:**

- DECISION: CREATE
- RESULT: "User is integrating Gemini API"
- Taxonomy: PROJECT / EPHEMERAL / CURRENT / TTL=14
- After 14 days: Auto-archived

---

#### A.4 Car Details (Merge)

**Input:**

```
USER: My Mitsubishi has tinted windows
```

**Expected:**

- SEARCH finds:
  - "User owns 2010 Toyota Corolla"
  - "Corolla has automatic gearbox"
  - "Car in Example City"
- DECISION: MERGE (3 facts)
- RESULT: "User owns 2010 Toyota Corolla with automatic gearbox, tinted windows, based in Example City"

---

#### A.5 RAG Echo (Discard)

**Input:**

```
ASSISTANT: You weighed 82 kg.
USER: Yes, exactly.
```

**Expected:**

- ANALYZE: ASSISTANT stated fact first (RAG recall)
- DECISION: DISCARD
- REASON: "RAG echo trap - User confirmed existing fact without new info"

---

### B. Glossary

| Term            | Definition                                                 |
| --------------- | ---------------------------------------------------------- |
| **Deliberate**  | Slow, thoughtful decision-making (vs fast reflexive)       |
| **Taxonomy**    | Classification system (Domain × Temporal × State)          |
| **TTL**         | Time-To-Live (days until fact expires)                     |
| **EPHEMERAL**   | Temporary state, auto-archives after 14 days               |
| **RAG Echo**    | LLM recalls fact from memory, user confirms → not new info |
| **Time Series** | Sequence of data points (e.g., weight measurements)        |
| **SCD Type 2**  | Slowly Changing Dimension (versioning strategy)            |
| **Hexagonal**   | Architecture pattern (Ports & Adapters)                    |

---

### C. Related RFCs

- [ACP v2 Simplified RFC](./ACP_V2_SIMPLIFIED_RFC.md) - Registry pattern for agents
- [Gmail Email Indexing RFC](./GMAIL_EMAIL_INDEXING_RFC.md) - Async task example

---

### D. References

**Firestore Vector Search:**

- [Firestore HNSW Documentation](https://cloud.google.com/firestore/docs/vector-search)
- [Multi-Vector RRF Search](../08_concepts/multi_vector_rrf_search.md)

**Agent Design:**

- [Agent Best Practices](../08_concepts/agent_best_practices.md)
- [Agent Business Logic](../08_concepts/agent_business_logic.md)

**Architecture:**

- [Hexagonal Architecture Patterns](../08_concepts/hexagonal_architecture_patterns.md)
- [Building Blocks Overview](../05_building_blocks/README.md)

---

**Last Updated:** 2026-02-16 (v2.2 - Architecture Finalization)  
**Status:** Proposed (Ready for Implementation)  
**Next Review:** 2026-02-23

---

## Version History

### v2.2 (2026-02-16) - Architecture Finalization + Reviewer Feedback

**Critical Architecture Additions:**

1. ✅ **Tool Call Architecture (Section 6)** - Added comprehensive multi-turn tool use specification
   - Provider-agnostic custom tools (not native)
   - Multi-turn conversation loop (similar to SmartResponseAgent)
   - Error handling and retry logic
   - Tool declaration format for Claude/Gemini adapters

2. ✅ **FactWriteService Integration (Section 7.3)** - Clarified relationship with existing services
   - Adapter reuses FactWriteService for embedding generation
   - New `skip_deduplication=True` parameter for v3 agent
   - Backward compatibility with v2 consolidation
   - Migration path documented

3. ✅ **Biographical Context Clarification** - Updated prompt comment
   - Removed fixed limits (50 facts + 15 anchors)
   - Added note about ConfigurationService customization
   - Clarified source: Biographical Context Cache with CRITICAL+HIGH priority filter

4. ✅ **Taxonomy_Enforcement Policy** - Removed hardcoded domain count
   - Changed from "9 options" to reference to taxonomy definition
   - Added 4th axis (Context Priority) to enforcement rules
   - Updated fallback defaults to include MEDIUM priority

**Reviewer Feedback Integration:**

- Addressed: Batch search optimization (marked as LOW priority - future optimization)
- Addressed: Explicit update mode flag (kept implicit mode detection, design decision documented)
- Addressed: Cold start context clarification (✅ FIXED - documented biographical cache limits)
- Addressed: Undo/restore mechanism (deferred to Future Work)
- Addressed: JSON validation in Report (confirmed - tool calls are source of truth)
- Addressed: Source field for conflicts (added to Future Work - needed for Gmail RFC)

**Technical Debt & Future Work:**

- LOW: Batch search optimization for N+1 problem (Phase 5)
- LOW: Optional explicit `mode` parameter in update_fact
- LOW: `source` field in metadata for document scanning integrations
- MEDIUM: Admin restore UI for INVALIDATED facts

---

### v2.1 (2026-02-16) - Critical Fixes

1. ✅ Added `context_priority` field to create_fact and merge_facts metadata (CRITICAL)
2. ✅ Formalized search_existing_facts return type with explicit fact_id: UUID (HIGH)
3. ✅ Documented update_fact dual modes: In-Place Update vs SCD Type 2 (HIGH)
4. ✅ Added Negation_And_Invalidation rule to conflict_resolution (MEDIUM)

---

### v2.0 (2026-02-16) - Initial Schema Redesign

1. 4-Dimensional Fact Taxonomy (Domain × Temporal × State × Priority)
2. 5 New Tools (search, create, update, merge, discard)
3. 8-Step Deliberate Cognitive Process
4. SCD Type 2 Versioning Strategy
5. Lifecycle Management with TTL

---

**RFC Status:** Ready for Phase 1 Implementation (Domain & Ports)
