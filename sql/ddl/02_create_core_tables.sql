CREATE TABLE core.authors (
    author_id       SERIAL PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    sinta_id        VARCHAR(50),
    scholar_id      VARCHAR(50),
    scopus_id       VARCHAR(50),
    program_study   VARCHAR(150),
    faculty         VARCHAR(150)
);

CREATE TABLE core.publications (
    publications_id SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    publication_date DATE,
    journal         VARCHAR(255),
    volume          VARCHAR(50),
    pages           VARCHAR(50),
    publisher       VARCHAR(255),
    description     TEXT,
    category        VARCHAR(100),
    doi             VARCHAR(100)
);

CREATE TABLE core.publication_authors (
    publication_id  INTEGER NOT NULL REFERENCES core.publications(publications_id),
    author_id       INTEGER NOT NULL REFERENCES core.authors(author_id),
    author_order    INTEGER NOT NULL,
    PRIMARY KEY (publication_id, author_id, author_order)
);

CREATE TABLE core.author_metrics (
    metric_id       SERIAL PRIMARY KEY,
    author_id       INTEGER NOT NULL REFERENCES core.authors(author_id),
    source          VARCHAR(50),
    h_index         INTEGER,
    score           NUMERIC,
    index_name      VARCHAR(100),
    retrieved_at    TIMESTAMP
);

CREATE TABLE core.publication_metrics (
    metric_id       SERIAL PRIMARY KEY,
    publication_id  INTEGER NOT NULL REFERENCES core.publications(publications_id),
    source          VARCHAR(50),
    citation_count  INTEGER,
    index_name      VARCHAR(100),
    score           NUMERIC,
    retrieved_at    TIMESTAMP
);