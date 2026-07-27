CREATE TABLE users (
    id serial PRIMARY KEY,
    name text NOT NULL,
    email text UNIQUE NOT NULL
);

CREATE TABLE products (
    id serial PRIMARY KEY,
    name text NOT NULL,
    price numeric(10, 2) NOT NULL
);

INSERT INTO users (name, email) VALUES
    ('Alice', 'alice@example.com'),
    ('Bob', 'bob@example.com');

INSERT INTO products (name, price) VALUES
    ('Widget A', 9.99),
    ('Widget B', 19.99);
