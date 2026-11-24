CC = qcc
TARGET = -Vgcc_ntox86_64
CFLAGS = $(TARGET) -g -Wall -Wextra -O1 -fno-pie
LDFLAGS = $(TARGET) -no-pie

all: np_agent brake_sim_buggy brake_sim_fixed

np_agent: np_agent.c np_protocol.h
	$(CC) $(CFLAGS) $(LDFLAGS) -o np_agent np_agent.c -lsocket

brake_sim_buggy: brake_sim_buggy.c brake_sim_core.h np_probe.h np_protocol.h
	$(CC) $(CFLAGS) $(LDFLAGS) -o brake_sim_buggy brake_sim_buggy.c

brake_sim_fixed: brake_sim_fixed.c brake_sim_core.h np_probe.h np_protocol.h
	$(CC) $(CFLAGS) $(LDFLAGS) -o brake_sim_fixed brake_sim_fixed.c

clean:
	rm -f np_agent brake_sim_buggy brake_sim_fixed
