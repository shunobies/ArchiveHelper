Archive Helper for Jellyfin Task List
--------------------------------------

Can you explain how the cleanup function works?

Please make all temporary mkv files be stored in a tmp directory in the user's area so that if something fails cleanup for the user if it requires manual cleanup can be done easily.

After successful Encoding Ask the user if they would like to cleanup the left over mkv files.

When creating the directories for movies and naming Movies make sure that these are Linux safe not requiring '' in the directory name making it easier for user if they have to manually go in and make modifications.


-------------------------
Look into OMDB API Key

------------------------
Create a settings menu
Allow for Entry of Connection SSH being it's own menu
Allow Directories to be it's own menu

---------------------------

The Stop button does not actually allow the application to start over. If an error occurs the stop button does nothing and you have to close the application and reopen it to attempt to recover from an error. Also if the application errors out during the MakeMKV process the log needs to be cleared otherwise the application can not recover from the error and start copying the DVD from the beginning.

-------------------

I have a few Movie DVD's that contain 4 movies on a Single DVD. I would like to add a check box for Multiple Titles for DVD. If it's checked allow the User to enter up to 4 movie titles and years and the MKV's extracted in order will be labeled with those titles.

--------------------

Add the ability to select Blu-ray instead of DVD would need to increase the cache from 128mb to 1024mb.
Add the ability to upload Books - Integrate Good Reads data to see if some of the Meta data can be pulled. I wonder how Calibre gets the Meta data for books?
--Subtask see if it's possible to pull books from Kindle and Convert them to epub and add them to the library.
Add the ability to pull Audible Books from your personal Audible collection convert them to MP3 and add them to the Jellyfin Library.
Add the ability to Rip and Classify Music CD's.
Look into auto generating cover.jpg or cover.png
Look into generating metadata.opf file for each book to help indexing.

---------------------

If the GUI is accidentally closed or there is a power outage and MakeMKV is in the middle of reading a disc the GUI doesn't seem to reattach to the session or the progress of the session.

If the power goes out on the server in the middle of a MakeMKV session will the GUI be able to restart the MakeMKV session or will the disc need to start from the beginning? If it needs to start over that's fine but some cleanup might be required to allow for the process to be restarted.

-------------

This system was designed for a spare laptop or desktop that has a DVD-ROM installed sitting around that you want to through some disks in and use as a Jellyfin server or even using a Raspberry Pi with a USB DVD-ROM. This script wasn't designed for renting a remote server that you don't have physical access to in my opinion that defeats the purpose the idea in my mind is to allow access to your physical media without the work of digging through it all for one movie, it also reduced wear and tear on your dvd collection. Big reason for me prevents the kids from digging around the DVD's and breaking them by mistake or getting the dreaded syrup from breakfast all over a disc. If you know you know.

