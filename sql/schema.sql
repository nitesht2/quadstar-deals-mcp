-- Quantdeal Database Schema
-- Run: psql quantdeal < sql/schema.sql

CREATE TABLE IF NOT EXISTS deals (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    original_price DECIMAL(10,2),
    deal_price DECIMAL(10,2) NOT NULL,
    discount_pct DECIMAL(5,2),
    retailer VARCHAR(100) NOT NULL,
    source_url TEXT NOT NULL,
    affiliate_url TEXT,
    image_url TEXT,
    category VARCHAR(100) DEFAULT 'tech',
    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    posted_at TIMESTAMP,
    is_posted BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    UNIQUE(source_url)
);

CREATE INDEX IF NOT EXISTS idx_deals_discount ON deals(discount_pct DESC);
CREATE INDEX IF NOT EXISTS idx_deals_posted ON deals(is_posted);
CREATE INDEX IF NOT EXISTS idx_deals_scraped ON deals(scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_deals_retailer ON deals(retailer);
