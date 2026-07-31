"""Fixture corpus — hand-authored, not extracted.

This exists so retrieval (and anything downstream of it) can be built and tested
without waiting for the ingestion pipeline, which does not exist yet at this
point in the build. It is deliberately synthetic, not real document content:

- **Deterministic** — every chunk_id derives from a fixed fake `doc_hash` and
  position, so re-seeding never produces different ids.
- **Near-duplicates, on purpose** — two pairs of chunks below say almost the
  same thing in different documents (excess baggage fees; dangerous goods
  restrictions). This is what exercises RRF's fusion behaviour and de-
  duplication across sub-questions, which a corpus of only-distinct content
  would never surface.
- **Rare tokens, on purpose** — clause numbers ("9.1"), route codes ("ABZ"),
  form numbers ("EY360A") appear verbatim. These mean almost nothing to an
  embedding model and are exactly what the keyword half of hybrid search exists
  to catch — see doc/components/02b-pgvector-postgresql.md §6.
- **Two collections** — `default` (passenger-facing) and `cargo`, so collection
  filtering has something real to prove itself against.

Thematically consistent with the real corpus in `data/raw/` (airline policy —
baggage, conditions of carriage, dangerous goods, loyalty) without being lifted
from it, since Docling hasn't run yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FixtureDocument:
    doc_hash: str
    collection: str
    source_file: str
    title: str
    description: str


@dataclass(frozen=True)
class FixtureChunk:
    doc_hash: str
    position: int
    page: int
    heading_path: str
    display_text: str
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def chunk_id(self) -> str:
        """Deterministic — position within a fixed fake document, nothing else.

        Real chunk ids (once E4 exists) derive from an actual file hash; here the
        `doc_hash` values below stand in for that, chosen to be stable and
        obviously synthetic (`fx-...`) rather than colliding with anything real.
        """
        return f"{self.doc_hash}__{self.position:03d}"

    @property
    def embedding_text(self) -> str:
        """Heading path + text — approximates what the real contextual
        preamble would produce, without a model call. Good enough for
        retrieval fixtures; not a substitute for the real thing in E4."""
        return f"{self.heading_path}: {self.display_text}"


DOCUMENTS: tuple[FixtureDocument, ...] = (
    FixtureDocument(
        doc_hash="fx-baggage-01",
        collection="default",
        source_file="fixture-baggage-policy.pdf",
        title="Baggage Policy",
        description="Checked and cabin baggage allowances, fees, and special items.",
    ),
    FixtureDocument(
        doc_hash="fx-coc-01",
        collection="default",
        source_file="fixture-conditions-of-carriage.pdf",
        title="General Conditions of Carriage",
        description="Passenger conditions of carriage — liability, refunds, notice periods.",
    ),
    FixtureDocument(
        doc_hash="fx-guest-01",
        collection="default",
        source_file="fixture-loyalty-terms.pdf",
        title="Guest Loyalty Programme Terms",
        description="Tier thresholds, mileage rules, and partner earning.",
    ),
    FixtureDocument(
        doc_hash="fx-dg-01",
        collection="default",
        source_file="fixture-dangerous-goods-guide.pdf",
        title="Dangerous Goods Guide",
        description="Forbidden and restricted items for checked and carry-on baggage.",
    ),
    FixtureDocument(
        doc_hash="fx-assist-01",
        collection="default",
        source_file="fixture-special-assistance.pdf",
        title="Special Assistance Policy",
        description="Medical clearance, wheelchair assistance, and service animals.",
    ),
    FixtureDocument(
        doc_hash="fx-cargo-coc-01",
        collection="cargo",
        source_file="fixture-cargo-conditions-of-carriage.pdf",
        title="Cargo Conditions of Carriage",
        description="Cargo liability limits and claims procedure.",
    ),
    FixtureDocument(
        doc_hash="fx-cargo-dg-01",
        collection="cargo",
        source_file="fixture-cargo-dangerous-goods.pdf",
        title="Cargo Dangerous Goods Regulations",
        description="Packing and labelling requirements for dangerous goods shipments.",
    ),
    FixtureDocument(
        doc_hash="fx-cargo-fees-01",
        collection="cargo",
        source_file="fixture-cargo-fee-schedule.pdf",
        title="Cargo Fee Schedule",
        description="Fuel surcharge, handling, and storage fees.",
    ),
)


CHUNKS: tuple[FixtureChunk, ...] = (
    # --- Baggage Policy (default) --------------------------------------------
    FixtureChunk(
        doc_hash="fx-baggage-01",
        position=0,
        page=3,
        heading_path="Baggage Policy > Checked Baggage > Economy",
        display_text=(
            "Economy Class passengers travelling on route ABZ-LHR are permitted one "
            "checked bag not exceeding 23kg. Bags exceeding this weight are subject "
            "to the excess baggage fee described in clause 4.2."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-baggage-01",
        position=1,
        page=3,
        heading_path="Baggage Policy > Excess Baggage > Fees",
        display_text=(
            "Excess baggage fees apply per kilogram over the free allowance, charged "
            "at the rate published for the passenger's route and cabin. Fees paid "
            "online at booking are discounted relative to airport payment."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-baggage-01",
        position=2,
        page=4,
        heading_path="Baggage Policy > Special Items > Sports Equipment",
        display_text=(
            "Sports equipment including golf bags and ski equipment is accepted as "
            "checked baggage under clause 4.2, subject to size limits and a "
            "supplementary handling fee where the item exceeds standard dimensions."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-baggage-01",
        position=3,
        page=2,
        heading_path="Baggage Policy > Cabin Baggage",
        display_text=(
            "One cabin bag not exceeding 7kg and one personal item may be carried "
            "into the cabin free of charge, subject to the dimensions published for "
            "the aircraft type operating the flight."
        ),
    ),
    # --- General Conditions of Carriage (default) ----------------------------
    FixtureChunk(
        doc_hash="fx-coc-01",
        position=0,
        page=12,
        heading_path="Conditions of Carriage > Liability > Clause 9.1",
        display_text=(
            "Clause 9.1: The carrier's liability for loss, delay, or damage to "
            "checked baggage is limited in accordance with the applicable "
            "international convention, unless a higher value has been declared."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-coc-01",
        position=1,
        page=15,
        heading_path="Conditions of Carriage > Refunds and Rebooking",
        display_text=(
            "A ticket may be rebooked or refunded subject to the fare conditions "
            "applicable at the time of purchase. Non-refundable fares may be "
            "eligible for a credit voucher, less any applicable change fee."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-coc-01",
        position=2,
        page=13,
        heading_path="Conditions of Carriage > Liability > Baggage",
        # Deliberate near-duplicate of fx-baggage-01/position 1 — same topic,
        # different document, different wording. Exercises RRF fusion and
        # cross-document deduplication.
        display_text=(
            "Where checked baggage exceeds the passenger's free weight allowance, "
            "an excess charge is payable at the rate in force for the ticketed "
            "route, calculated per kilogram of excess weight carried."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-coc-01",
        position=3,
        page=16,
        heading_path="Conditions of Carriage > Notice Periods",
        display_text=(
            "Claims for delayed or damaged baggage must be notified to the carrier "
            "in writing within seven days of receipt for damage, or twenty-one days "
            "for delay, measured from the date the baggage was made available."
        ),
    ),
    # --- Guest Loyalty Programme Terms (default) -----------------------------
    FixtureChunk(
        doc_hash="fx-guest-01",
        position=0,
        page=2,
        heading_path="Loyalty Terms > Tiers > Thresholds",
        display_text=(
            "Silver tier requires 18,750 tier miles, Gold requires 37,500 tier "
            "miles, and Platinum requires 93,750 tier miles within a rolling "
            "twelve-month qualification period."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-guest-01",
        position=1,
        page=4,
        heading_path="Loyalty Terms > Miles > Expiry",
        display_text=(
            "Guest miles expire thirty-six months after the month in which they "
            "were earned unless the member has qualifying account activity, "
            "except for Platinum and Emerald tier members whose miles do not expire."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-guest-01",
        position=2,
        page=6,
        heading_path="Loyalty Terms > Partners > Earning",
        display_text=(
            "Miles earned through partner hotel and car rental bookings are "
            "credited within ten business days and are subject to the partner's "
            "own published earning rate, not the airline's base rate."
        ),
    ),
    # --- Dangerous Goods Guide (default) --------------------------------------
    FixtureChunk(
        doc_hash="fx-dg-01",
        position=0,
        page=1,
        heading_path="Dangerous Goods Guide > Forbidden > Checked Baggage",
        display_text=(
            "Reference EY360A: Spare lithium battery packs, aerosols exceeding "
            "500ml, and flammable liquids such as petrol are forbidden in checked "
            "baggage under all circumstances."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-dg-01",
        position=1,
        page=1,
        heading_path="Dangerous Goods Guide > Forbidden > Carry-on",
        display_text=(
            "Reference EY360A: Disabling devices including tasers and pepper spray, "
            "and any item classified as an explosive or pyrotechnic, are forbidden "
            "in carry-on baggage without exception."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-dg-01",
        position=2,
        page=2,
        heading_path="Dangerous Goods Guide > Restricted > Spare Batteries",
        display_text=(
            "Spare lithium batteries not exceeding 100Wh may be carried in cabin "
            "baggage only, individually protected against short circuit, with a "
            "maximum of twenty spare batteries per passenger."
        ),
    ),
    # --- Special Assistance Policy (default) ----------------------------------
    FixtureChunk(
        doc_hash="fx-assist-01",
        position=0,
        page=1,
        heading_path="Special Assistance > Medical Clearance",
        display_text=(
            "Passengers with a condition that may affect their fitness to fly must "
            "obtain a medical clearance form completed by their physician no more "
            "than ten days before travel."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-assist-01",
        position=1,
        page=2,
        heading_path="Special Assistance > Wheelchair",
        display_text=(
            "Wheelchair assistance must be requested at least forty-eight hours "
            "before departure to guarantee availability at both the departure and "
            "arrival airports."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-assist-01",
        position=2,
        page=3,
        heading_path="Special Assistance > Service Animals",
        display_text=(
            "Trained service animals accompanying a passenger with a disability are "
            "carried free of charge in the cabin, subject to the destination "
            "country's import documentation requirements."
        ),
    ),
    # --- Cargo Conditions of Carriage (cargo) ---------------------------------
    FixtureChunk(
        doc_hash="fx-cargo-coc-01",
        position=0,
        page=5,
        heading_path="Cargo Conditions of Carriage > Liability",
        display_text=(
            "The carrier's liability for cargo loss or damage is limited per "
            "kilogram in accordance with the applicable convention, unless the "
            "shipper has declared a higher value and paid the associated charge."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-cargo-coc-01",
        position=1,
        page=7,
        heading_path="Cargo Conditions of Carriage > Dangerous Goods Acceptance",
        # Deliberate near-duplicate of fx-dg-01/position 0 — same restriction,
        # cargo framing instead of passenger framing.
        display_text=(
            "Shipments containing lithium batteries, aerosols, or flammable "
            "liquids are accepted for carriage only where packed, marked, and "
            "documented in accordance with the dangerous goods regulations."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-cargo-coc-01",
        position=2,
        page=9,
        heading_path="Cargo Conditions of Carriage > Claims Procedure",
        display_text=(
            "Claims for cargo loss or damage must be submitted in writing within "
            "fourteen days of delivery, accompanied by the airway bill and "
            "commercial invoice for the shipment."
        ),
    ),
    # --- Cargo Dangerous Goods Regulations (cargo) -----------------------------
    FixtureChunk(
        doc_hash="fx-cargo-dg-01",
        position=0,
        page=2,
        heading_path="Cargo Dangerous Goods > Packing Requirements",
        display_text=(
            "Dangerous goods consignments must be packed in UN-specification "
            "packaging appropriate to the packing group assigned to the substance "
            "being shipped."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-cargo-dg-01",
        position=1,
        page=3,
        heading_path="Cargo Dangerous Goods > Labelling Requirements",
        display_text=(
            "Each package must display the correct hazard class label, UN number, "
            "and proper shipping name in a size and location visible without "
            "moving other packages in the consignment."
        ),
    ),
    # --- Cargo Fee Schedule (cargo) --------------------------------------------
    FixtureChunk(
        doc_hash="fx-cargo-fees-01",
        position=0,
        page=1,
        heading_path="Cargo Fee Schedule > Fuel Surcharge",
        display_text=(
            "A fuel surcharge is applied per kilogram of chargeable weight and is "
            "reviewed monthly based on the average jet fuel price for the preceding "
            "period."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-cargo-fees-01",
        position=1,
        page=2,
        heading_path="Cargo Fee Schedule > Handling Fee",
        display_text=(
            "A handling fee applies to every airway bill processed, covering "
            "acceptance, documentation, and build-up, independent of the "
            "shipment's chargeable weight."
        ),
    ),
    FixtureChunk(
        doc_hash="fx-cargo-fees-01",
        position=2,
        page=2,
        heading_path="Cargo Fee Schedule > Storage Fee",
        display_text=(
            "Storage fees apply to shipments not collected within forty-eight "
            "hours of notified arrival, charged per day per hundred kilograms of "
            "chargeable weight."
        ),
    ),
)


def chunk_by_id(chunk_id: str) -> FixtureChunk:
    for chunk in CHUNKS:
        if chunk.chunk_id == chunk_id:
            return chunk
    raise KeyError(chunk_id)


def document_by_hash(doc_hash: str) -> FixtureDocument:
    for doc in DOCUMENTS:
        if doc.doc_hash == doc_hash:
            return doc
    raise KeyError(doc_hash)
