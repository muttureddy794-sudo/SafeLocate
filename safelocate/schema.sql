-- Optional: the app creates these tables itself via `flask --app app init-db`.
CREATE DATABASE IF NOT EXISTS safelocate CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE safelocate;

CREATE TABLE IF NOT EXISTS users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(80) NOT NULL,
  email VARCHAR(190) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  email_verified TINYINT(1) NOT NULL DEFAULT 0,
  sharing_on TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS otps (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_id INT NOT NULL,
  purpose VARCHAR(12) NOT NULL,
  code_hash CHAR(64) NOT NULL,
  expires_at DATETIME NOT NULL,
  attempts INT NOT NULL DEFAULT 0,
  used TINYINT(1) NOT NULL DEFAULT 0,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS shares (
  id INT AUTO_INCREMENT PRIMARY KEY,
  owner_id INT NOT NULL,
  contact_email VARCHAR(190) NOT NULL,
  status VARCHAR(12) NOT NULL DEFAULT 'pending', -- pending|active|declined|revoked|expired
  duration_min INT NOT NULL,
  created_at DATETIME NOT NULL,
  invite_expires_at DATETIME NOT NULL,
  expires_at DATETIME NULL,
  INDEX (contact_email), INDEX (owner_id),
  FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Latest position only. No location history is ever stored.
CREATE TABLE IF NOT EXISTS locations (
  owner_id INT PRIMARY KEY,
  lat DOUBLE NOT NULL,
  lng DOUBLE NOT NULL,
  accuracy DOUBLE NULL,
  updated_at DATETIME NOT NULL,
  FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;
