ALTER TABLE production_briefs ADD COLUMN ad_contacts TEXT NOT NULL DEFAULT '{}'
    CHECK (length(ad_contacts) <= 512 AND json_valid(ad_contacts)
        AND (ad_contacts = '{}' OR ad_cta <> ''));
