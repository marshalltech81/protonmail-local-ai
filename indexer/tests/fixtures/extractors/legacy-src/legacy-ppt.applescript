-- Generate the synthetic legacy .ppt fixture (#957) with Microsoft PowerPoint.
-- Usage: osascript legacy-ppt.applescript /absolute/path/legacy.ppt
-- Run through generate-ppt.sh, which then replaces the "Last Saved By"
-- name PowerPoint writes from the signed-in account on save.
on run argv
	set outPath to item 1 of argv
	tell application "Microsoft PowerPoint"
		activate
		set deck to make new presentation
		-- Slide 1: title and content; text in the placeholders.
		set s1 to make new slide at end of deck with properties {layout:slide layout text slide}
		set content of text range of text frame of shape 1 of s1 to "Synthetic legacy slide deck"
		set content of text range of text frame of shape 2 of s1 to "The AMBER-KESTREL project code is 5129." & return & "Café crème at the Zürich office, naïve résumé."
		-- Slide 2: blank, with a free text box.
		set s2 to make new slide at end of deck with properties {layout:slide layout blank}
		set box to make new shape at end of s2 with properties {auto shape type:autoshape rectangle, left position:72, top:72, width:400, height:100}
		set content of text range of text frame of box to "The text box holds TEAL-MARMOT 3307."
		-- The author would otherwise be the signed-in account's name.
		set value of document property "Author" of deck to "Synthetic Author"
		save deck in (POSIX file outPath) as save as presentation
		close active presentation saving no
	end tell
end run
