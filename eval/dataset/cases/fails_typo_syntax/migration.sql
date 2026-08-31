ALTER TABLE users ADD COLUMN nickname text;
ALTER TABLE users ADD CONSTRAIN users_nickname_len CHECK (length(nickname) < 40);
