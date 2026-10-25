--
-- PostgreSQL database dump
--

\restrict XAhP3xzt5rT1xeNwtym4vMVQiBi1TXJlRjwY5zRWkQsMOSk8KfYf8aKLKmBXvJF

-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: core; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA core;


--
-- Name: mart; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA mart;


--
-- Name: raw; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA raw;


--
-- Name: staging; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA staging;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: author_metrics; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.author_metrics (
    metric_id integer NOT NULL,
    author_id integer NOT NULL,
    source character varying(50),
    h_index integer,
    score numeric,
    index_name character varying(100),
    retrieved_at timestamp without time zone
);


--
-- Name: author_metrics_metric_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

CREATE SEQUENCE core.author_metrics_metric_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: author_metrics_metric_id_seq; Type: SEQUENCE OWNED BY; Schema: core; Owner: -
--

ALTER SEQUENCE core.author_metrics_metric_id_seq OWNED BY core.author_metrics.metric_id;


--
-- Name: authors; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.authors (
    author_id integer NOT NULL,
    name character varying(255) NOT NULL,
    sinta_id character varying(50),
    scholar_id character varying(50),
    scopus_id character varying(50),
    program_study character varying(150),
    faculty character varying(150)
);


--
-- Name: authors_author_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

CREATE SEQUENCE core.authors_author_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: authors_author_id_seq; Type: SEQUENCE OWNED BY; Schema: core; Owner: -
--

ALTER SEQUENCE core.authors_author_id_seq OWNED BY core.authors.author_id;


--
-- Name: publication_authors; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.publication_authors (
    publication_id integer NOT NULL,
    author_id integer,
    author_order integer NOT NULL,
    author_name text,
    author_type text,
    publication_author_id integer NOT NULL,
    CONSTRAINT publication_authors_name_required_without_author_id CHECK (((author_id IS NOT NULL) OR (NULLIF(btrim(author_name), ''::text) IS NOT NULL)))
);


--
-- Name: publication_authors_publication_author_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

ALTER TABLE core.publication_authors ALTER COLUMN publication_author_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.publication_authors_publication_author_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: publication_metrics; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.publication_metrics (
    metric_id integer NOT NULL,
    publication_id integer NOT NULL,
    source character varying(50),
    citation_count integer,
    index_name character varying(100),
    score numeric,
    retrieved_at timestamp without time zone,
    profile_author_id integer
);


--
-- Name: publication_metrics_metric_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

CREATE SEQUENCE core.publication_metrics_metric_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: publication_metrics_metric_id_seq; Type: SEQUENCE OWNED BY; Schema: core; Owner: -
--

ALTER SEQUENCE core.publication_metrics_metric_id_seq OWNED BY core.publication_metrics.metric_id;


--
-- Name: publications; Type: TABLE; Schema: core; Owner: -
--

CREATE TABLE core.publications (
    publication_id integer NOT NULL,
    title text NOT NULL,
    publication_date date,
    journal character varying(255),
    volume character varying(50),
    pages character varying(50),
    publisher character varying(255),
    description text,
    category character varying(100),
    doi character varying(100),
    publication_year integer,
    issue character varying(50),
    link text
);


--
-- Name: publications_publications_id_seq; Type: SEQUENCE; Schema: core; Owner: -
--

CREATE SEQUENCE core.publications_publications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: publications_publications_id_seq; Type: SEQUENCE OWNED BY; Schema: core; Owner: -
--

ALTER SEQUENCE core.publications_publications_id_seq OWNED BY core.publications.publication_id;


--
-- Name: journal_catalog; Type: TABLE; Schema: raw; Owner: -
--

CREATE TABLE raw.journal_catalog (
    journal_id bigint NOT NULL,
    journal_original text NOT NULL,
    journal_normalized text NOT NULL,
    issn text,
    eissn text,
    identifiers jsonb NOT NULL,
    classification text NOT NULL,
    classification_reason text NOT NULL,
    publication_origins jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT journal_catalog_classification_check CHECK ((classification = ANY (ARRAY['journal'::text, 'ambiguous'::text])))
);


--
-- Name: journal_catalog_journal_id_seq; Type: SEQUENCE; Schema: raw; Owner: -
--

CREATE SEQUENCE raw.journal_catalog_journal_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: journal_catalog_journal_id_seq; Type: SEQUENCE OWNED BY; Schema: raw; Owner: -
--

ALTER SEQUENCE raw.journal_catalog_journal_id_seq OWNED BY raw.journal_catalog.journal_id;


--
-- Name: journal_index_verification; Type: TABLE; Schema: raw; Owner: -
--

CREATE TABLE raw.journal_index_verification (
    verification_id bigint NOT NULL,
    journal_id bigint NOT NULL,
    source text NOT NULL,
    status text NOT NULL,
    indexed boolean,
    matched_identity jsonb DEFAULT '{}'::jsonb NOT NULL,
    sjr jsonb DEFAULT '[]'::jsonb NOT NULL,
    sinta_rank text,
    sinta_period text,
    evidence_url text,
    checked_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    reason text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT journal_index_verification_source_check CHECK ((source = ANY (ARRAY['scopus'::text, 'scimagojr'::text, 'sinta'::text]))),
    CONSTRAINT journal_index_verification_status_check CHECK ((status = ANY (ARRAY['verified'::text, 'unverified'::text, 'not_found'::text, 'access_unavailable'::text, 'error'::text]))),
    CONSTRAINT journal_index_verification_verified_check CHECK (((status <> 'verified'::text) OR ((indexed IS TRUE) AND (evidence_url IS NOT NULL))))
);


--
-- Name: journal_index_verification_verification_id_seq; Type: SEQUENCE; Schema: raw; Owner: -
--

ALTER TABLE raw.journal_index_verification ALTER COLUMN verification_id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.journal_index_verification_verification_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: scholar_author; Type: TABLE; Schema: raw; Owner: -
--

CREATE TABLE raw.scholar_author (
    raw_author_id integer NOT NULL,
    result_id integer NOT NULL,
    urutan integer DEFAULT 0 NOT NULL,
    scholar_id character varying(50) NOT NULL,
    nama text NOT NULL,
    h_index integer,
    domain text,
    batches integer,
    elapsed numeric,
    prodi text
);


--
-- Name: scholar_author_raw_author_id_seq; Type: SEQUENCE; Schema: raw; Owner: -
--

CREATE SEQUENCE raw.scholar_author_raw_author_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scholar_author_raw_author_id_seq; Type: SEQUENCE OWNED BY; Schema: raw; Owner: -
--

ALTER SEQUENCE raw.scholar_author_raw_author_id_seq OWNED BY raw.scholar_author.raw_author_id;


--
-- Name: scholar_publication; Type: TABLE; Schema: raw; Owner: -
--

CREATE TABLE raw.scholar_publication (
    raw_publication_id integer NOT NULL,
    result_id integer NOT NULL,
    urutan integer NOT NULL,
    scholar_id character varying(50) NOT NULL,
    title text,
    authors text,
    venue text,
    extra text,
    citations text,
    year text,
    publication_year integer,
    detail_url text,
    detail_title text,
    detail_authors text,
    detail_date text,
    detail_venue text,
    detail_volume text,
    detail_issue text,
    detail_pages text,
    detail_publisher text,
    detail_description text,
    detail_citations text,
    detail_link text,
    detail_pdf_link text,
    status text,
    detail_doi text,
    profile_volume text,
    profile_issue text,
    profile_pages text
);


--
-- Name: scholar_publication_author; Type: TABLE; Schema: raw; Owner: -
--

CREATE TABLE raw.scholar_publication_author (
    raw_publication_author_id integer NOT NULL,
    raw_publication_id integer NOT NULL,
    author_order integer NOT NULL,
    author_name text NOT NULL,
    author_role text NOT NULL,
    profile_owner_status text NOT NULL,
    scholar_id character varying(50),
    sumber text NOT NULL,
    daftar_terpotong boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);


--
-- Name: scholar_publication_author_raw_publication_author_id_seq; Type: SEQUENCE; Schema: raw; Owner: -
--

CREATE SEQUENCE raw.scholar_publication_author_raw_publication_author_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scholar_publication_author_raw_publication_author_id_seq; Type: SEQUENCE OWNED BY; Schema: raw; Owner: -
--

ALTER SEQUENCE raw.scholar_publication_author_raw_publication_author_id_seq OWNED BY raw.scholar_publication_author.raw_publication_author_id;


--
-- Name: scholar_publication_raw_publication_id_seq; Type: SEQUENCE; Schema: raw; Owner: -
--

CREATE SEQUENCE raw.scholar_publication_raw_publication_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scholar_publication_raw_publication_id_seq; Type: SEQUENCE OWNED BY; Schema: raw; Owner: -
--

ALTER SEQUENCE raw.scholar_publication_raw_publication_id_seq OWNED BY raw.scholar_publication.raw_publication_id;


--
-- Name: scholar_scrape_result; Type: TABLE; Schema: raw; Owner: -
--

CREATE TABLE raw.scholar_scrape_result (
    result_id integer NOT NULL,
    scholar_id character varying(50) NOT NULL,
    scraped_at timestamp without time zone DEFAULT now() NOT NULL,
    run_id uuid,
    status character varying(20),
    raw_envelope jsonb NOT NULL
);


--
-- Name: scholar_scrape_result_result_id_seq; Type: SEQUENCE; Schema: raw; Owner: -
--

CREATE SEQUENCE raw.scholar_scrape_result_result_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scholar_scrape_result_result_id_seq; Type: SEQUENCE OWNED BY; Schema: raw; Owner: -
--

ALTER SEQUENCE raw.scholar_scrape_result_result_id_seq OWNED BY raw.scholar_scrape_result.result_id;


--
-- Name: stg_authors; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.stg_authors (
    stg_author_id integer NOT NULL,
    result_id integer NOT NULL,
    urutan integer DEFAULT 0 NOT NULL,
    scholar_id character varying(50) NOT NULL,
    nama text NOT NULL,
    h_index integer,
    domain text,
    batches integer,
    elapsed numeric
);


--
-- Name: stg_authors_stg_author_id_seq; Type: SEQUENCE; Schema: staging; Owner: -
--

CREATE SEQUENCE staging.stg_authors_stg_author_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: stg_authors_stg_author_id_seq; Type: SEQUENCE OWNED BY; Schema: staging; Owner: -
--

ALTER SEQUENCE staging.stg_authors_stg_author_id_seq OWNED BY staging.stg_authors.stg_author_id;


--
-- Name: stg_publications; Type: TABLE; Schema: staging; Owner: -
--

CREATE TABLE staging.stg_publications (
    stg_publication_id integer NOT NULL,
    result_id integer NOT NULL,
    urutan integer NOT NULL,
    scholar_id character varying(50) NOT NULL,
    title text NOT NULL,
    publication_year integer,
    publication_date date,
    journal character varying(255),
    volume character varying(50),
    pages character varying(50),
    issue character varying(50),
    publisher character varying(255),
    description text,
    category character varying(100),
    link text,
    doi character varying(100),
    authors text,
    author_source character varying(10)
);


--
-- Name: stg_publications_stg_publication_id_seq; Type: SEQUENCE; Schema: staging; Owner: -
--

CREATE SEQUENCE staging.stg_publications_stg_publication_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: stg_publications_stg_publication_id_seq; Type: SEQUENCE OWNED BY; Schema: staging; Owner: -
--

ALTER SEQUENCE staging.stg_publications_stg_publication_id_seq OWNED BY staging.stg_publications.stg_publication_id;


--
-- Name: author_metrics metric_id; Type: DEFAULT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.author_metrics ALTER COLUMN metric_id SET DEFAULT nextval('core.author_metrics_metric_id_seq'::regclass);


--
-- Name: authors author_id; Type: DEFAULT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.authors ALTER COLUMN author_id SET DEFAULT nextval('core.authors_author_id_seq'::regclass);


--
-- Name: publication_metrics metric_id; Type: DEFAULT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.publication_metrics ALTER COLUMN metric_id SET DEFAULT nextval('core.publication_metrics_metric_id_seq'::regclass);


--
-- Name: publications publication_id; Type: DEFAULT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.publications ALTER COLUMN publication_id SET DEFAULT nextval('core.publications_publications_id_seq'::regclass);


--
-- Name: journal_catalog journal_id; Type: DEFAULT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.journal_catalog ALTER COLUMN journal_id SET DEFAULT nextval('raw.journal_catalog_journal_id_seq'::regclass);


--
-- Name: scholar_author raw_author_id; Type: DEFAULT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_author ALTER COLUMN raw_author_id SET DEFAULT nextval('raw.scholar_author_raw_author_id_seq'::regclass);


--
-- Name: scholar_publication raw_publication_id; Type: DEFAULT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_publication ALTER COLUMN raw_publication_id SET DEFAULT nextval('raw.scholar_publication_raw_publication_id_seq'::regclass);


--
-- Name: scholar_publication_author raw_publication_author_id; Type: DEFAULT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_publication_author ALTER COLUMN raw_publication_author_id SET DEFAULT nextval('raw.scholar_publication_author_raw_publication_author_id_seq'::regclass);


--
-- Name: scholar_scrape_result result_id; Type: DEFAULT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_scrape_result ALTER COLUMN result_id SET DEFAULT nextval('raw.scholar_scrape_result_result_id_seq'::regclass);


--
-- Name: stg_authors stg_author_id; Type: DEFAULT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.stg_authors ALTER COLUMN stg_author_id SET DEFAULT nextval('staging.stg_authors_stg_author_id_seq'::regclass);


--
-- Name: stg_publications stg_publication_id; Type: DEFAULT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.stg_publications ALTER COLUMN stg_publication_id SET DEFAULT nextval('staging.stg_publications_stg_publication_id_seq'::regclass);


--
-- Name: author_metrics author_metrics_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.author_metrics
    ADD CONSTRAINT author_metrics_pkey PRIMARY KEY (metric_id);


--
-- Name: authors authors_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.authors
    ADD CONSTRAINT authors_pkey PRIMARY KEY (author_id);


--
-- Name: publication_authors publication_authors_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.publication_authors
    ADD CONSTRAINT publication_authors_pkey PRIMARY KEY (publication_author_id);


--
-- Name: publication_authors publication_authors_publication_order_key; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.publication_authors
    ADD CONSTRAINT publication_authors_publication_order_key UNIQUE (publication_id, author_order);


--
-- Name: publication_metrics publication_metrics_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.publication_metrics
    ADD CONSTRAINT publication_metrics_pkey PRIMARY KEY (metric_id);


--
-- Name: publications publications_pkey; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.publications
    ADD CONSTRAINT publications_pkey PRIMARY KEY (publication_id);


--
-- Name: publications uq_publications_title_publication_year; Type: CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.publications
    ADD CONSTRAINT uq_publications_title_publication_year UNIQUE NULLS NOT DISTINCT (title, publication_year);


--
-- Name: journal_catalog journal_catalog_pkey; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.journal_catalog
    ADD CONSTRAINT journal_catalog_pkey PRIMARY KEY (journal_id);


--
-- Name: journal_index_verification journal_index_verification_pkey; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.journal_index_verification
    ADD CONSTRAINT journal_index_verification_pkey PRIMARY KEY (verification_id);


--
-- Name: scholar_author scholar_author_pkey; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_author
    ADD CONSTRAINT scholar_author_pkey PRIMARY KEY (raw_author_id);


--
-- Name: scholar_publication_author scholar_publication_author_pkey; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_publication_author
    ADD CONSTRAINT scholar_publication_author_pkey PRIMARY KEY (raw_publication_author_id);


--
-- Name: scholar_publication scholar_publication_pkey; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_publication
    ADD CONSTRAINT scholar_publication_pkey PRIMARY KEY (raw_publication_id);


--
-- Name: scholar_scrape_result scholar_scrape_result_pkey; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_scrape_result
    ADD CONSTRAINT scholar_scrape_result_pkey PRIMARY KEY (result_id);


--
-- Name: scholar_scrape_result scholar_scrape_result_run_id_key; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_scrape_result
    ADD CONSTRAINT scholar_scrape_result_run_id_key UNIQUE (run_id);


--
-- Name: journal_catalog uq_journal_catalog_title; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.journal_catalog
    ADD CONSTRAINT uq_journal_catalog_title UNIQUE (journal_normalized);


--
-- Name: journal_index_verification uq_journal_verification_source; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.journal_index_verification
    ADD CONSTRAINT uq_journal_verification_source UNIQUE (journal_id, source);


--
-- Name: scholar_author uq_raw_scholar_author_result_urutan; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_author
    ADD CONSTRAINT uq_raw_scholar_author_result_urutan UNIQUE (result_id, urutan);


--
-- Name: scholar_publication_author uq_raw_scholar_publication_author_pub_urutan; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_publication_author
    ADD CONSTRAINT uq_raw_scholar_publication_author_pub_urutan UNIQUE (raw_publication_id, author_order);


--
-- Name: scholar_publication uq_raw_scholar_publication_result_urutan; Type: CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_publication
    ADD CONSTRAINT uq_raw_scholar_publication_result_urutan UNIQUE (result_id, urutan);


--
-- Name: stg_authors stg_authors_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.stg_authors
    ADD CONSTRAINT stg_authors_pkey PRIMARY KEY (stg_author_id);


--
-- Name: stg_publications stg_publications_pkey; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.stg_publications
    ADD CONSTRAINT stg_publications_pkey PRIMARY KEY (stg_publication_id);


--
-- Name: stg_authors uq_stg_authors_result_urutan; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.stg_authors
    ADD CONSTRAINT uq_stg_authors_result_urutan UNIQUE (result_id, urutan);


--
-- Name: stg_publications uq_stg_publications_result_urutan; Type: CONSTRAINT; Schema: staging; Owner: -
--

ALTER TABLE ONLY staging.stg_publications
    ADD CONSTRAINT uq_stg_publications_result_urutan UNIQUE (result_id, urutan);


--
-- Name: uq_authors_name_normalized; Type: INDEX; Schema: core; Owner: -
--

CREATE UNIQUE INDEX uq_authors_name_normalized ON core.authors USING btree (lower(btrim((name)::text)));


--
-- Name: ix_raw_scholar_scrape_result_scholar_id; Type: INDEX; Schema: raw; Owner: -
--

CREATE INDEX ix_raw_scholar_scrape_result_scholar_id ON raw.scholar_scrape_result USING btree (scholar_id);


--
-- Name: ix_raw_scholar_scrape_result_scraped_at; Type: INDEX; Schema: raw; Owner: -
--

CREATE INDEX ix_raw_scholar_scrape_result_scraped_at ON raw.scholar_scrape_result USING btree (scraped_at);


--
-- Name: author_metrics author_metrics_author_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.author_metrics
    ADD CONSTRAINT author_metrics_author_id_fkey FOREIGN KEY (author_id) REFERENCES core.authors(author_id);


--
-- Name: publication_authors publication_authors_author_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.publication_authors
    ADD CONSTRAINT publication_authors_author_id_fkey FOREIGN KEY (author_id) REFERENCES core.authors(author_id);


--
-- Name: publication_authors publication_authors_publication_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.publication_authors
    ADD CONSTRAINT publication_authors_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES core.publications(publication_id);


--
-- Name: publication_metrics publication_metrics_profile_author_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.publication_metrics
    ADD CONSTRAINT publication_metrics_profile_author_id_fkey FOREIGN KEY (profile_author_id) REFERENCES core.authors(author_id);


--
-- Name: publication_metrics publication_metrics_publication_id_fkey; Type: FK CONSTRAINT; Schema: core; Owner: -
--

ALTER TABLE ONLY core.publication_metrics
    ADD CONSTRAINT publication_metrics_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES core.publications(publication_id);


--
-- Name: journal_index_verification journal_index_verification_journal_id_fkey; Type: FK CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.journal_index_verification
    ADD CONSTRAINT journal_index_verification_journal_id_fkey FOREIGN KEY (journal_id) REFERENCES raw.journal_catalog(journal_id);


--
-- Name: scholar_author scholar_author_result_id_fkey; Type: FK CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_author
    ADD CONSTRAINT scholar_author_result_id_fkey FOREIGN KEY (result_id) REFERENCES raw.scholar_scrape_result(result_id);


--
-- Name: scholar_publication_author scholar_publication_author_raw_publication_id_fkey; Type: FK CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_publication_author
    ADD CONSTRAINT scholar_publication_author_raw_publication_id_fkey FOREIGN KEY (raw_publication_id) REFERENCES raw.scholar_publication(raw_publication_id);


--
-- Name: scholar_publication scholar_publication_result_id_fkey; Type: FK CONSTRAINT; Schema: raw; Owner: -
--

ALTER TABLE ONLY raw.scholar_publication
    ADD CONSTRAINT scholar_publication_result_id_fkey FOREIGN KEY (result_id) REFERENCES raw.scholar_scrape_result(result_id);


--
-- PostgreSQL database dump complete
--

\unrestrict XAhP3xzt5rT1xeNwtym4vMVQiBi1TXJlRjwY5zRWkQsMOSk8KfYf8aKLKmBXvJF
