-- ========================================
-- Seed data for testing / development
-- ========================================

-- Users
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

INSERT INTO users (name, email) VALUES
    ('Alice', 'alice@example.com'),
    ('Bob', 'bob@example.com'),
    ('Charlie', 'charlie@example.com'),
    ('Diana', 'diana@example.com'),
    ('Eve', 'eve@example.com');

-- Products
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    price NUMERIC(10, 2) NOT NULL,
    stock INT DEFAULT 0
);

INSERT INTO products (name, price, stock) VALUES
    ('Widget A', 9.99, 100),
    ('Widget B', 19.99, 50),
    ('Gadget X', 49.99, 25),
    ('Gadget Y', 99.99, 10),
    ('Thingamajig', 4.99, 200);

-- Orders
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id),
    product_id INT REFERENCES products(id),
    quantity INT NOT NULL DEFAULT 1,
    ordered_at TIMESTAMPTZ DEFAULT now()
);

INSERT INTO orders (user_id, product_id, quantity) VALUES
    (1, 1, 2),
    (1, 3, 1),
    (2, 2, 5),
    (3, 5, 10),
    (4, 4, 1),
    (5, 1, 3),
    (5, 2, 2);
