-- The "Gaming industry" catalog entry (key 'industry') stops counting toward a server's
-- 10-game limit. Migration 005's trigger counted every row, so a server with ten games
-- couldn't follow it. The replacement skips that key on both sides: following 'industry'
-- is never refused, and its row isn't counted when the next game is checked. The error
-- text is unchanged on purpose; repo.follow_game and repo.apply_guild_setup look for it.
-- A second trigger covers UPDATE: once one row is free, renaming it to a game (or moving a
-- game's row to a full server) could make an eleventh counted game without an INSERT.
-- Before this file no UPDATE could raise a server's count, so nothing guarded it.
-- No rows are touched, and this runs in the migration runner's one transaction.
DROP TRIGGER guild_games_limit;

CREATE TRIGGER guild_games_limit BEFORE INSERT ON guild_games
WHEN NEW.game_key <> 'industry'
    AND (SELECT COUNT(*) FROM guild_games
         WHERE guild_id = NEW.guild_id AND game_key <> 'industry') >= 10
BEGIN
    SELECT RAISE(ABORT, 'at most 10 games per guild');
END;

CREATE TRIGGER guild_games_limit_update BEFORE UPDATE OF guild_id, game_key ON guild_games
WHEN NEW.game_key <> 'industry'
    AND (OLD.game_key = 'industry' OR OLD.guild_id <> NEW.guild_id)
    AND (SELECT COUNT(*) FROM guild_games
         WHERE guild_id = NEW.guild_id AND game_key <> 'industry') >= 10
BEGIN
    SELECT RAISE(ABORT, 'at most 10 games per guild');
END;
